"""Lock Base Classes."""

import random
from types import TracebackType
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import BaseModel, Field
from typing_extensions import Doc

from grelmicro.coordination._handle import LockHandle
from grelmicro.coordination._protocol import LockPrimitive
from grelmicro.coordination._tokens import generate_worker_id
from grelmicro.coordination.errors import CoordinationSettingsValidationError

# Seam for randomness in retry jitter. Tests pin it to a fixed value.
_random = random.random


def jittered_interval(base: float, jitter: float) -> float:
    """Return base scaled by uniform(1-jitter, 1+jitter), or base when jitter is 0."""
    if jitter:
        return base * (1.0 + jitter * (2.0 * _random() - 1.0))
    return base


class BaseLockConfig(BaseModel, frozen=True, extra="forbid"):
    """Base Lock Config."""

    worker: Annotated[
        str | UUID,
        Doc("""
            The worker identity.

            By default, a UUIDv1 is generated.
            """),
        Field(default_factory=generate_worker_id),
    ]


def assert_worker_unchanged(
    current: BaseLockConfig, new: BaseLockConfig
) -> None:
    """Reject a reconfigure that would change the immutable `worker` field."""
    if new.worker != current.worker:
        msg = (
            f"the worker is immutable and cannot change "
            f"({current.worker!r} -> {new.worker!r}). "
            f"Reuse the existing worker on the new config."
        )
        raise CoordinationSettingsValidationError(msg)


class BaseLock(LockPrimitive, Protocol):
    """Base Lock Protocol."""

    async def __aenter__(self) -> LockHandle:
        """Acquire the lock.

        Returns the `LockHandle` for this acquisition.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
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

    async def acquire(self) -> LockHandle:
        """Acquire the lock.

        Returns the `LockHandle` for this acquisition.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        ...

    async def acquire_nowait(self) -> LockHandle:
        """
        Acquire the lock, without blocking.

        Returns the `LockHandle` for this acquisition.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
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
