"""Skeleton third-party grelmicro adapter for MongoDB.

Shows how an external package registers a Provider and an Adapter under
grelmicro's entry-point groups (see this package's `pyproject.toml`). The
lock methods are stubs: fill them in with real MongoDB calls to ship a
working backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from grelmicro.providers import Provider

if TYPE_CHECKING:
    from types import TracebackType


class MongoLockAdapter:
    """A `LockBackend` backed by MongoDB (stubbed)."""

    def __init__(self, provider: MongoProvider) -> None:
        """Bind the adapter to the provider it borrows."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the adapter."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the adapter."""

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire the lock (implement with a MongoDB upsert + TTL index).

        Returns the fencing token when granted, `None` when another token
        already holds the lock.
        """
        raise NotImplementedError

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock when the token matches the holder."""
        raise NotImplementedError

    async def locked(self, *, name: str) -> bool:
        """Return whether the lock is currently held."""
        raise NotImplementedError

    async def owned(self, *, name: str, token: str) -> bool:
        """Return whether the token currently owns the lock."""
        raise NotImplementedError


class MongoProvider(Provider):
    """A `Provider` that owns a MongoDB client (stubbed)."""

    short_name = "mongo"

    def __init__(self, url: str) -> None:
        """Store the connection URL (open the real client in `__aenter__`)."""
        self._url = url

    def lock(self, **kwargs: Any) -> MongoLockAdapter:  # noqa: ANN401, ARG002
        """Build the matching lock adapter bound to this provider."""
        return MongoLockAdapter(self)

    async def __aenter__(self) -> Self:
        """Open the MongoDB client."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the MongoDB client."""
