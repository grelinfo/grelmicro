"""TTL Cache."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from typing_extensions import Doc

from grelmicro.cache._backends import get_cache_backend
from grelmicro.cache._protocol import CacheBackend


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


_SENTINEL = object()
_CACHE_PREFIX = "cache"


class TTLCache:
    """Cache with per-entry TTL and optional LRU eviction.

    Delegates storage to a ``CacheBackend`` (in-memory, Redis, etc.).
    TTLCache handles maxsize enforcement, LRU eviction, serialization,
    and statistics on top of the backend.

    When no backend is provided, the registered default is used
    (see ``MemoryCacheBackend`` or ``RedisCacheBackend``).

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
            Callable[[Any], bytes] | None,
            Doc(
                """
                Optional function to serialize values to bytes
                before storing in the backend.
                """,
            ),
        ] = None,
        deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                """
                Optional function to deserialize bytes from the
                backend back into values.
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
        if (serializer is None) != (deserializer is None):
            msg = "serializer and deserializer must be provided together"
            raise ValueError(msg)

        self._maxsize = maxsize
        self._ttl = ttl
        self._backend = backend
        self._serializer = serializer
        self._deserializer = deserializer
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        # LRU tracking (ordered list of keys, most recent at end)
        self._keys: list[str] = []

    def _get_backend(self) -> CacheBackend:
        """Resolve the backend (lazy, to allow registration after construction)."""
        if self._backend is None:
            self._backend = get_cache_backend()
        return self._backend

    def _serialize(self, value: Any) -> bytes:  # noqa: ANN401
        """Serialize a value to bytes for storage."""
        if self._serializer is not None:
            return self._serializer(value)
        if isinstance(value, bytes):
            return value
        msg = (
            f"Cannot store {type(value).__name__} without a serializer. "
            f"Pass serializer/deserializer to TTLCache or use bytes."
        )
        raise TypeError(msg)

    def _deserialize(self, raw: bytes) -> Any:  # noqa: ANN401
        """Deserialize bytes from storage."""
        if self._deserializer is not None:
            return self._deserializer(raw)
        return raw

    async def get(
        self,
        key: str,
        default: Any = _SENTINEL,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Get a value by key.

        Returns the default if the key is missing or expired.
        A hit promotes the key in LRU order.

        Returns:
            The cached value, or None if not found and no default given.
        """
        raw = await self._get_backend().get(key=f"{_CACHE_PREFIX}:{key}")
        if raw is None:
            self._misses += 1
            return None if default is _SENTINEL else default
        self._hits += 1
        if self._maxsize > 0:
            self._promote(key)
        return self._deserialize(raw)

    async def set(
        self,
        key: str,
        value: Any,  # noqa: ANN401
        ttl: float | None = None,
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

    async def delete(self, key: str) -> None:
        """Delete a key from the cache.

        No-op if the key does not exist.
        """
        await self._get_backend().delete(key=f"{_CACHE_PREFIX}:{key}")
        if key in self._keys:
            self._keys.remove(key)

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
        if key in self._keys:
            self._keys.remove(key)
        self._keys.append(key)

    async def _evict(self) -> None:
        """Evict the least recently used entry (first in key list)."""
        if not self._keys:  # pragma: no cover
            return
        lru_key = self._keys.pop(0)
        await self._get_backend().delete(key=f"{_CACHE_PREFIX}:{lru_key}")
        self._evictions += 1
