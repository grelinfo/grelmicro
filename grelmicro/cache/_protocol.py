"""Cache Backend Protocol."""

from types import TracebackType
from typing import Protocol, Self


class CacheBackend(Protocol):
    """Protocol for cache storage backends.

    All methods are async because backends typically perform I/O.
    Backends are pure key-value stores: TTL, eviction, and statistics
    are managed by ``TTLCache``.
    """

    async def __aenter__(self) -> Self:
        """Open the backend connection."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend connection."""
        ...

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        ...

    async def set(self, *, key: str, value: bytes, ttl: float) -> None:
        """Store raw bytes with a TTL in seconds."""
        ...

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent)."""
        ...

    async def clear(self) -> None:
        """Remove all entries managed by this backend."""
        ...
