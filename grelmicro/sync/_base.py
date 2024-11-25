"""Grelmicro Lock API."""

from types import TracebackType
from typing import Annotated, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from typing_extensions import Doc

from grelmicro.sync.abc import Synchronization


class BaseLockConfig(BaseModel):
    """Base Lock Config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[
        str,
        Doc("""
            The name of the resource to lock.
            """),
    ]
    worker: Annotated[
        str | UUID,
        Doc("""
            The worker identity.

            By default, use a UUIDv1.
            """),
    ]


class BaseLock(Synchronization, Protocol):
    """Base Lock Protocol."""

    async def __aenter__(self) -> Self:
        """Acquire the lock.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        ...

    @property
    def config(self) -> BaseLockConfig:
        """Return the config."""
        ...

    async def acquire(self) -> None:
        """Acquire the lock.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        ...

    async def acquire_nowait(self) -> None:
        """
        Acquire the lock, without blocking.

        Raises:
            WouldBlock: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        ...

    async def release(self) -> None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        ...

    async def locked(self) -> bool:
        """Check if the lock is currently held."""
        ...

    async def owned(self) -> bool:
        """Check if the lock is currently held by the current token."""
        ...
