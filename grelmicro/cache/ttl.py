"""TTL Cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Generic, cast

from pydantic import BaseModel, NonNegativeInt, PositiveFloat
from typing_extensions import Doc, TypeVar

from grelmicro._app import Grelmicro
from grelmicro.cache._stampede import (
    _SENTINEL,
    AsyncStampedeGuard,
    compute_with_stampede,
)
from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.cache.serializers import CacheSerializer

T = TypeVar("T", default=Any)


class TTLCacheConfig(BaseModel, frozen=True, extra="forbid"):
    """Frozen snapshot of the `TTLCache` declarative settings.

    Carries the settings that round-trip in serialized form. Runtime
    dependencies (`backend`, `serializer`) stay as constructor kwargs
    on `TTLCache` since they are object references, not values.
    """

    maxsize: Annotated[
        NonNegativeInt,
        Doc(
            """
            Maximum number of entries tracked locally for LRU eviction.
            `0` disables the cap and the LRU bookkeeping. Only enforced
            in-process: a shared backend may still hold more entries.
            """,
        ),
    ] = 0

    ttl: Annotated[
        PositiveFloat,
        Doc(
            """
            Default TTL in seconds applied when `set` is called without
            a per-entry override.
            """,
        ),
    ] = 60


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

# Suffix for the stale-reserve copy stored next to a value. The copy
# outlives the value by `stale_ttl` seconds so `get_or_set` and `@cached`
# can serve it when a later recompute fails. The `\x1f` separator stays
# out of band (no real key uses it) and, unlike `\x00`, is valid in a
# Postgres text key.
_STALE_SUFFIX = "\x1fst"

# Suffix for the XFetch metadata sidecar written by `@cached(early=...)`.
# It carries the last recompute timing next to a value and shares the
# same out-of-band `\x1f` separator.
_XFETCH_SUFFIX = "\x1fxf"


class TTLCache(Generic[T]):
    """Cache with per-entry TTL and optional LRU eviction.

    Delegates storage to a ``CacheBackend`` (in-memory, Redis, etc.).
    TTLCache handles maxsize enforcement, LRU eviction, serialization,
    and statistics on top of the backend.

    When no backend is provided, the registered default is used
    (see ``MemoryCacheAdapter`` or ``RedisCacheAdapter``).

    The type parameter ``T`` represents the cached value type.
    Defaults to ``Any`` when unspecified (``TTLCache()``).
    Use ``TTLCache[User](serializer=PydanticSerializer(User))``
    for typed caching.

    Raises:
        pydantic.ValidationError: If `maxsize` is negative or `ttl` is not
            positive. `ValidationError` is a subclass of `ValueError`, so
            existing `except ValueError:` blocks still catch it.
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

                - ``PydanticSerializer(Model)``: Type-safe Pydantic roundtrips.
                - ``JsonSerializer()``: JSON-native types (dict, list, etc.).
                - ``PickleSerializer()``: Any picklable object. Trusted
                  backends only: deserialization can execute arbitrary code.
                - ``None``: Raw bytes only (no serialization).
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the cache."""
        self._config = TTLCacheConfig(maxsize=maxsize, ttl=ttl)
        # Snapshot the validated config to instance attributes so the
        # hot path (`get`, `set`) reads a single attribute instead of
        # walking through `self._config.<field>`.
        self._maxsize = self._config.maxsize
        self._ttl = self._config.ttl
        self._backend = backend
        self._serializer = serializer
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        # LRU tracking (OrderedDict for O(1) move_to_end / popitem)
        self._keys: OrderedDict[str, None] = OrderedDict()
        # Per-key in-process locks for get_or_set stampede protection.
        self._stampede = AsyncStampedeGuard()

    @property
    def config(self) -> TTLCacheConfig:
        """Return the frozen config snapshot."""
        return self._config

    def _get_backend(self) -> CacheBackend:
        """Resolve the backend on every call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the active `Grelmicro` app is
        consulted via `Grelmicro.current()` so that
        `micro.override(Cache(...))` blocks take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryCacheAdapter()` for a per-process
                cache), register a `Cache` Component, or run the call
                inside `async with micro:` or after
                `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            NoActiveAppError,
        )
        from grelmicro.errors import OutOfContextError  # noqa: PLC0415

        try:
            cache = Grelmicro.current().get("cache", "default")
        except (NoActiveAppError, ComponentNotRegisteredError):
            msg = (
                "TTLCache resolved no backend. Pass backend= "
                "(MemoryCacheAdapter() for a per-process cache), register "
                "a Cache component, or run the call inside `async with micro:` "
                "or after `micro.install(app)`."
            )
            raise OutOfContextError(msg) from None
        return cache.backend

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
            _emit.incr("grelmicro.cache.operations", result="miss")
            return default
        self._hits += 1
        _emit.incr("grelmicro.cache.operations", result="hit")
        if self._maxsize > 0:
            self._promote(key)
        return self._deserialize(raw)

    async def _peek(self, key: str, default: T | None = None) -> T | None:
        """Read a key without recording hit/miss stats or LRU promotion.

        Used for the double-checked recheck inside stampede locks, where
        the lookup is part of an in-flight miss rather than a new access.
        """
        raw = await self._get_backend().get(key=f"{_CACHE_PREFIX}:{key}")
        if raw is None:
            return default
        return self._deserialize(raw)

    async def _read_stale(self, key: str, default: T | None = None) -> T | None:
        """Read the stale-reserve copy of a key, or `default` when absent.

        The copy is written by `set` when `stale_ttl` is given and lives
        `stale_ttl` seconds past the value's TTL. Pass a sentinel as
        `default` to tell a stored `None` apart from an absent copy.
        """
        raw = await self._get_backend().get(
            key=f"{_CACHE_PREFIX}:{key}{_STALE_SUFFIX}"
        )
        if raw is None:
            return default
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
        *,
        tags: Annotated[
            Sequence[str],
            Doc(
                "Tags to associate with the entry. Invalidate every entry"
                " sharing a tag at once with `delete_tags`."
            ),
        ] = (),
        stale_ttl: Annotated[
            float | None,
            Doc(
                "Extra seconds to keep a fallback copy of the value past"
                " its TTL. When set, `get_or_set` and `@cached` serve this"
                " stale copy if a later recompute fails, until the extra"
                " budget elapses. `None` (the default) keeps no fallback."
            ),
        ] = None,
    ) -> None:
        """Set a value with an optional per-entry TTL override and tags.

        If the cache is full (maxsize > 0), evicts the least recently
        used entry before storing.

        Raises:
            ValueError: If ttl or stale_ttl is not positive.
            TypeError: If value is not bytes and no serializer is set.
        """
        if ttl is not None and ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)
        if stale_ttl is not None and stale_ttl <= 0:
            msg = "stale_ttl must be positive"
            raise ValueError(msg)

        entry_ttl = ttl if ttl is not None else self._ttl
        raw = self._serialize(value)

        # Evict if at capacity and this is a new key
        if self._maxsize > 0 and key not in self._keys:
            while len(self._keys) >= self._maxsize:
                await self._evict()

        await self._get_backend().set(
            key=f"{_CACHE_PREFIX}:{key}", value=raw, ttl=entry_ttl, tags=tags
        )
        if stale_ttl is not None:
            # Carry the same tags so `delete_tags` invalidates the reserve
            # alongside the value it shadows.
            await self._get_backend().set(
                key=f"{_CACHE_PREFIX}:{key}{_STALE_SUFFIX}",
                value=raw,
                ttl=entry_ttl + stale_ttl,
                tags=tags,
            )

        if self._maxsize > 0:
            self._promote(key)

    async def get_or_set(
        self,
        key: Annotated[str, Doc("The cache key.")],
        factory: Annotated[
            Callable[[], T] | Callable[[], Awaitable[T]],
            Doc(
                "Sync or async callable that produces the value on a miss."
                " Awaited when it returns a coroutine."
            ),
        ],
        *,
        ttl: Annotated[
            float | None,
            Doc("Per-entry TTL override in seconds. Uses the default if None."),
        ] = None,
        tags: Annotated[
            Sequence[str],
            Doc("Tags to associate with the entry when it is computed."),
        ] = (),
        stale_ttl: Annotated[
            float | None,
            Doc(
                "Extra seconds to keep a fallback copy past the TTL. When"
                " set and the ``factory`` raises on a miss, the most recent"
                " value is served instead of propagating the error, until"
                " the extra budget elapses. `None` (default) propagates."
            ),
        ] = None,
    ) -> T:
        """Return the cached value, or compute, store, and return it.

        On a hit the cached value is returned and a hit is recorded. On a
        miss the ``factory`` runs once under stampede protection, the
        result is stored with the given ``ttl`` and ``tags``, and a miss
        is recorded by the initial read. Concurrent misses on the same
        key fold into a single computation, across replicas when a lock
        backend is configured.

        With ``stale_ttl`` set, a ``factory`` that raises on a miss serves
        the most recent value (kept for ``stale_ttl`` seconds past its TTL)
        instead of propagating the error.
        """
        result = await self.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return cast("T", result)

        async def compute() -> T:
            value = factory()
            if asyncio.iscoroutine(value):
                value = await value
            await self.set(
                key, cast("T", value), ttl, tags=tags, stale_ttl=stale_ttl
            )
            return cast("T", value)

        if stale_ttl is None:
            return cast(
                "T",
                await compute_with_stampede(
                    self,
                    key,
                    compute,
                    self._stampede,
                    per_key=True,
                    auto_distributed=True,
                ),
            )

        try:
            return cast(
                "T",
                await compute_with_stampede(
                    self,
                    key,
                    compute,
                    self._stampede,
                    per_key=True,
                    auto_distributed=True,
                ),
            )
        except Exception:  # serve stale on any recompute failure
            stale = await self._read_stale(key, _SENTINEL)
            if stale is not _SENTINEL:
                _emit.incr("grelmicro.cache.stale_serves")
                return cast("T", stale)
            raise

    async def get_many(
        self,
        keys: Annotated[Sequence[str], Doc("The cache keys to read.")],
    ) -> dict[str, T]:
        """Return a dict of found values for the given keys.

        Missing or expired keys are absent from the result. Records one
        hit per found key and one miss per absent key.
        """
        keys = list(keys)
        if not keys:
            return {}
        raw = await self._get_backend().get_many(
            keys=[f"{_CACHE_PREFIX}:{key}" for key in keys]
        )
        plen = len(_CACHE_PREFIX) + 1
        found = {full[plen:]: value for full, value in raw.items()}
        result: dict[str, T] = {}
        for key in keys:
            if key not in found:
                self._misses += 1
                _emit.incr("grelmicro.cache.operations", result="miss")
                continue
            self._hits += 1
            _emit.incr("grelmicro.cache.operations", result="hit")
            if self._maxsize > 0:
                self._promote(key)
            result[key] = self._deserialize(found[key])
        return result

    async def set_many(
        self,
        mapping: Annotated[
            Mapping[str, T],
            Doc("Key to value pairs to store."),
        ],
        *,
        ttl: Annotated[
            float | None,
            Doc("Per-entry TTL override in seconds. Uses the default if None."),
        ] = None,
        tags: Annotated[
            Sequence[str],
            Doc("Tags to associate with every stored entry."),
        ] = (),
    ) -> None:
        """Store many key to value pairs with one TTL and optional tags.

        Raises:
            ValueError: If ttl is not positive.
            TypeError: If a value is not bytes and no serializer is set.
        """
        if ttl is not None and ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)
        if not mapping:
            return

        entry_ttl = ttl if ttl is not None else self._ttl
        items = {
            f"{_CACHE_PREFIX}:{key}": self._serialize(value)
            for key, value in mapping.items()
        }

        if self._maxsize > 0:
            for key in mapping:
                if key not in self._keys:
                    while len(self._keys) >= self._maxsize:
                        await self._evict()
                self._promote(key)

        await self._get_backend().set_many(
            items=items, ttl=entry_ttl, tags=tags
        )

    async def delete(
        self,
        key: Annotated[str, Doc("The cache key to delete.")],
    ) -> None:
        """Delete a key from the cache.

        Also drops the stale-reserve copy, so an explicit delete is never
        undone by a later stale serve. No-op if the key does not exist.
        """
        await self._get_backend().delete_many(
            keys=[
                f"{_CACHE_PREFIX}:{key}",
                f"{_CACHE_PREFIX}:{key}{_STALE_SUFFIX}",
            ]
        )
        self._keys.pop(key, None)

    async def delete_many(
        self,
        keys: Annotated[Sequence[str], Doc("The cache keys to delete.")],
    ) -> None:
        """Delete many keys from the cache.

        Keys that do not exist are ignored.
        """
        keys = list(keys)
        if not keys:
            return
        await self._get_backend().delete_many(
            keys=[f"{_CACHE_PREFIX}:{key}" for key in keys]
            + [f"{_CACHE_PREFIX}:{key}{_STALE_SUFFIX}" for key in keys]
        )
        for key in keys:
            self._keys.pop(key, None)

    async def delete_tags(
        self,
        *tags: Annotated[str, Doc("Tags whose every entry should be deleted.")],
    ) -> None:
        """Delete every entry associated with any of the given tags.

        Clears the local LRU bookkeeping, since the deleted keys are not
        known in advance.
        """
        if not tags:
            return
        await self._get_backend().delete_tags(tags=tags)
        self._keys.clear()

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
        await self._get_backend().delete_many(
            keys=[
                f"{_CACHE_PREFIX}:{lru_key}",
                f"{_CACHE_PREFIX}:{lru_key}{_STALE_SUFFIX}",
                f"{_CACHE_PREFIX}:{lru_key}{_XFETCH_SUFFIX}",
            ]
        )
        self._evictions += 1
