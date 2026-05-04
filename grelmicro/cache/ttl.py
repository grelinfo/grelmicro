"""TTL Cache."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Generic

from typing_extensions import Doc, TypeVar

from grelmicro.cache._backends import get_cache_backend

if TYPE_CHECKING:
    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.cache.serializers import CacheSerializer

T = TypeVar("T", default=Any)


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """Cache statistics snapshot.

    Attributes:
        hits: Number of cache hits.
        misses: Number of cache misses.
        maxsize: Maximum number of entries (``0`` means unlimited).
        currsize: Current number of tracked entries.
        evictions: Number of entries evicted to make room.
    """

    hits: int
    misses: int
    maxsize: int
    currsize: int
    evictions: int


_CACHE_PREFIX = "cache"


class TTLCache(Generic[T]):
    """Cache with per-entry TTL and optional LRU eviction.

    Delegates storage to a ``CacheBackend`` (in-memory, Redis, etc.).
    TTLCache handles maxsize enforcement, LRU eviction, serialization,
    and statistics on top of the backend.

    When no backend is provided, the registered default is used
    (see ``MemoryCacheBackend`` or ``RedisCacheBackend``).

    The type parameter ``T`` represents the cached value type.
    Defaults to ``Any`` when unspecified (``TTLCache()``).
    Use ``TTLCache[User](serializer=PydanticSerializer(User))``
    for typed caching.

    Raises:
        ValueError: If maxsize is negative or ttl is not positive.
    """

    def __init__(
        self,
        maxsize: Annotated[
            int,
            Doc(
                """
                Maximum number of entries. ``0`` means unlimited.
                Only enforced locally (not by the backend).
                """,
            ),
        ] = 0,
        ttl: Annotated[
            float,
            Doc(
                """
                Default TTL in seconds for all entries.
                """,
            ),
        ] = 60,
        *,
        backend: Annotated[
            CacheBackend | None,
            Doc(
                """
                The cache storage backend.

                By default, the registered cache backend is used.
                """,
            ),
        ] = None,
        serializer: Annotated[
            CacheSerializer[T] | None,
            Doc(
                """
                Serialization strategy for cached values.

                Any object implementing the ``CacheSerializer`` protocol
                (``dumps`` / ``loads`` methods) can be used.

                Built-in options:

                - ``PickleSerializer()``: Any picklable object.
                - ``JsonSerializer()``: JSON-native types (dict, list, etc.).
                - ``PydanticSerializer(Model)``: Type-safe Pydantic roundtrips.
                - ``None``: Raw bytes only (no serialization).
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the cache."""
        if maxsize < 0:
            msg = "maxsize must be non-negative"
            raise ValueError(msg)
        if ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)

        self._maxsize = maxsize
        self._ttl = ttl
        self._backend = backend
        self._serializer = serializer
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        # LRU tracking (OrderedDict for O(1) move_to_end / popitem)
        self._keys: OrderedDict[str, None] = OrderedDict()

    def _get_backend(self) -> CacheBackend:
        """Resolve the backend (lazy, to allow registration after construction)."""
        if self._backend is None:
            self._backend = get_cache_backend()
        return self._backend

    def _serialize(self, value: T) -> bytes:
        """Serialize a value to bytes for storage."""
        if self._serializer is not None:
            return self._serializer.dumps(value)  # type: ignore[arg-type]
        if isinstance(value, bytes):
            return value
        msg = (
            f"Cannot store {type(value).__name__} without a serializer. "
            f"Pass a serializer to TTLCache or use bytes."
        )
        raise TypeError(msg)

    def _deserialize(self, raw: bytes) -> T:
        """Deserialize bytes from storage."""
        if self._serializer is not None:
            return self._serializer.loads(raw)  # type: ignore[return-value]
        return raw  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

    async def get(
        self,
        key: Annotated[str, Doc("The cache key.")],
        default: Annotated[
            T | None,
            Doc("Value to return if the key is missing or expired."),
        ] = None,
    ) -> T | None:
        """Get a value by key.

        Returns the default if the key is missing or expired.
        A hit promotes the key in LRU order.
        """
        raw = await self._get_backend().get(key=f"{_CACHE_PREFIX}:{key}")
        if raw is None:
            self._misses += 1
            return default
        self._hits += 1
        if self._maxsize > 0 and key in self._keys:
            self._keys.move_to_end(key)
        return self._deserialize(raw)

    async def set(
        self,
        key: Annotated[str, Doc("The cache key.")],
        value: Annotated[
            T,
            Doc("The value to store. Must be bytes or serializable."),
        ],
        ttl: Annotated[
            float | None,
            Doc(
                "Per-entry TTL override in seconds. Uses the default TTL if None."
            ),
        ] = None,
    ) -> None:
        """Set a value with an optional per-entry TTL override.

        If the cache is full (maxsize > 0), evicts the least recently
        used entry before storing.

        Raises:
            ValueError: If ttl is not positive.
            TypeError: If value is not bytes and no serializer is set.
        """
        if ttl is not None and ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)

        entry_ttl = ttl if ttl is not None else self._ttl
        raw = self._serialize(value)

        # Evict if at capacity and this is a new key
        if self._maxsize > 0 and key not in self._keys:
            while len(self._keys) >= self._maxsize:
                await self._evict()

        await self._get_backend().set(
            key=f"{_CACHE_PREFIX}:{key}", value=raw, ttl=entry_ttl
        )

        if self._maxsize > 0:
            self._promote(key)

    async def delete(
        self,
        key: Annotated[str, Doc("The cache key to delete.")],
    ) -> None:
        """Delete a key from the cache.

        No-op if the key does not exist.
        """
        await self._get_backend().delete(key=f"{_CACHE_PREFIX}:{key}")
        self._keys.pop(key, None)

    async def clear(self) -> None:
        """Remove all entries from the cache."""
        await self._get_backend().clear()
        self._keys.clear()

    def cache_info(self) -> CacheInfo:
        """Return a snapshot of cache statistics."""
        return CacheInfo(
            hits=self._hits,
            misses=self._misses,
            maxsize=self._maxsize,
            currsize=len(self._keys),
            evictions=self._evictions,
        )

    def _promote(self, key: str) -> None:
        """Move a key to the most-recently-used position."""
        self._keys[key] = None
        self._keys.move_to_end(key)

    async def _evict(self) -> None:
        """Evict the least recently used entry (first in key list)."""
        if not self._keys:  # pragma: no cover
            return
        lru_key, _ = self._keys.popitem(last=False)
        await self._get_backend().delete(key=f"{_CACHE_PREFIX}:{lru_key}")
        self._evictions += 1
