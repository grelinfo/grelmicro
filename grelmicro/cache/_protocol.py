"""Cache Protocol."""

from typing import Any

from typing_extensions import Protocol, runtime_checkable

from grelmicro.cache.ttl import CacheInfo


@runtime_checkable
class Cache(Protocol):
    """Protocol for simple in-process caches (e.g. ``TTLCache``).

    Sync methods for caches that do not perform I/O.
    Can be used with both sync and async decorated functions.
    """

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        """Get a value by key."""
        ...

    def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Set a value."""
        ...

    def clear(self) -> None:
        """Remove all entries."""
        ...

    def cache_info(self) -> CacheInfo:
        """Return cache statistics."""
        ...


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for cache backends that perform I/O (e.g. ``RedisCache``).

    Can only be used with async decorated functions.
    """

    async def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        """Get a value by key."""
        ...

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Set a value."""
        ...

    async def clear(self) -> None:
        """Remove all entries."""
        ...

    def cache_info(self) -> CacheInfo:
        """Return cache statistics."""
        ...
