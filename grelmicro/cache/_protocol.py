"""Cache Backend Protocol.

This module defines a `typing.Protocol`. Methods end with `...`
because the protocol describes a structural contract, not an
implementation. Concrete backends (`RedisCacheAdapter`,
`MemoryCacheAdapter`, `PostgresCacheAdapter`) provide the bodies.
"""

from types import TracebackType
from typing import Annotated, Protocol, Self, runtime_checkable

from typing_extensions import Doc


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for cache storage backends.

    All methods are async because backends typically perform I/O.
    Backends are pure key-value stores: TTL, eviction, and statistics
    are managed by ``TTLCache``.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so the sync ``@cached`` wrapper can
    dispatch coroutines back into it.
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

    async def get(
        self,
        *,
        key: Annotated[
            str,
            Doc("Fully qualified cache key, already namespaced by `TTLCache`."),
        ],
    ) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        ...

    async def set(
        self,
        *,
        key: Annotated[
            str,
            Doc("Fully qualified cache key, already namespaced by `TTLCache`."),
        ],
        value: Annotated[
            bytes,
            Doc("Serialized payload to store. Opaque to the backend."),
        ],
        ttl: Annotated[
            float,
            Doc(
                "Time-to-live in seconds. The backend must drop the entry once"
                " this many seconds have elapsed since the write."
            ),
        ],
    ) -> None:
        """Store raw bytes with a TTL in seconds."""
        ...

    async def delete(
        self,
        *,
        key: Annotated[
            str,
            Doc("Fully qualified cache key. No-op if absent."),
        ],
    ) -> None:
        """Delete a key (no-op if absent)."""
        ...

    async def clear(self) -> None:
        """Remove all entries managed by this backend."""
        ...
