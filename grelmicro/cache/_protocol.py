"""Cache Protocol."""

from typing import Any

from typing_extensions import Protocol, runtime_checkable

from grelmicro.cache.ttl import CacheInfo


@runtime_checkable
class Cache(Protocol):
    """Protocol for synchronous cache backends used by the ``@cached`` decorator.

    Any object that implements ``get``, ``set``, ``clear``, and
    ``cache_info`` can be used as a cache backend.
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
class AsyncCache(Protocol):
    """Protocol for asynchronous cache backends used by the ``@cached`` decorator.

    Any object that implements async ``get``, ``set``, ``clear``, and
    sync ``cache_info`` can be used as an async cache backend.

    Async cache backends can only be used with async decorated functions.
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
