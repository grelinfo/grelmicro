"""Synchronization Abstract Base Classes and Protocols."""

from types import TracebackType
from typing import Annotated, Protocol, Self, runtime_checkable

from pydantic import PositiveFloat
from typing_extensions import Doc


@runtime_checkable
class SyncBackend(Protocol):
    """Synchronization Backend Protocol.

    This is the low-level API for the distributed synchronization backend that is platform agnostic.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so sync adapters (``Lock.from_thread``,
    ``TaskLock.from_thread``) can dispatch coroutines back into it.
    """

    async def __aenter__(self) -> Self:
        """Open the synchronization backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the synchronization backend."""
        ...

    async def acquire(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to acquire."),
        ],
        token: Annotated[
            str,
            Doc(
                "Caller-supplied ownership token. The same token must"
                " be passed to `release` and `owned`."
            ),
        ],
        duration: Annotated[
            float,
            Doc(
                "Seconds the lock is held before the backend may release"
                " it automatically. The acquirer should renew before"
                " this elapses."
            ),
        ],
    ) -> bool:
        """Acquire the lock.

        Returns `True` when the lock was granted, `False` when another
        token already holds it.
        """
        ...

    async def release(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to release."),
        ],
        token: Annotated[
            str,
            Doc(
                "Ownership token previously passed to `acquire`. The"
                " backend only releases when the token matches."
            ),
        ],
    ) -> bool:
        """Release the lock.

        Returns `True` when the lock was released, `False` when the
        token did not own the lock.
        """
        ...

    async def locked(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to inspect."),
        ],
    ) -> bool:
        """Return `True` when the lock is currently held by any token."""
        ...

    async def owned(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to inspect."),
        ],
        token: Annotated[
            str,
            Doc("Ownership token to compare against the current holder."),
        ],
    ) -> bool:
        """Return `True` when the lock is currently held by `token`."""
        ...


@runtime_checkable
class SyncPrimitive(Protocol):
    """Synchronization Primitive Protocol."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the synchronization primitive."""
        ...


Seconds = PositiveFloat
