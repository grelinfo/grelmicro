"""Cache Protocol."""

from typing import Any

from typing_extensions import Protocol, runtime_checkable

from grelmicro.cache.ttl import CacheInfo


@runtime_checkable
class Cache(Protocol):
    """Protocol for cache backends used by the ``@cached`` decorator.

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
