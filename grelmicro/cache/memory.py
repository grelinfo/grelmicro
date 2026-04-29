"""Memory Cache Backend."""

from time import monotonic
from types import TracebackType
from typing import Annotated, Self

from typing_extensions import Doc

from grelmicro.cache._backends import cache_backend_registry


class MemoryCacheBackend:
    """In-memory cache backend.

    Stores entries in a Python dict with lazy TTL expiry.
    Suitable for testing and single-process applications.
    """

    def __init__(
        self,
        *,
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register this cache backend in the "
                "backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the memory cache backend."""
        self._data: dict[str, tuple[bytes, float]] = {}
        self._auto_registered = auto_register
        if auto_register:
            cache_backend_registry.set(self)

    async def __aenter__(self) -> Self:
        """Open the cache backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the cache backend."""
        self._data.clear()
        if (
            self._auto_registered
            and cache_backend_registry.is_loaded
            and cache_backend_registry.get() is self
        ):
            cache_backend_registry.reset()

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        Expired entries are removed lazily on access.
        """
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if monotonic() >= expiry:
            del self._data[key]
            return None
        return value

    async def set(self, *, key: str, value: bytes, ttl: float) -> None:
        """Store raw bytes with a TTL in seconds."""
        self._data[key] = (value, monotonic() + ttl)

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent)."""
        self._data.pop(key, None)

    async def clear(self) -> None:
        """Remove all entries."""
        self._data.clear()
