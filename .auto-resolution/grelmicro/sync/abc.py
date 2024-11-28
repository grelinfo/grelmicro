"""Grelmicro Synchronization Abstract Base Classes and Protocols."""

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from pydantic import PositiveFloat


class SyncBackend(Protocol):
    """Synchronization Backend Protocol.

    This is the low level API for the distributed lock backend that is platform agnostic.
    """

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend."""
        ...

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire the lock.

        Args:
            name: The name of the lock.
            token: The token to acquire the lock.
            duration: The duration in seconds to hold the lock.

        Returns:
            True if the lock is acquired, False if the lock is already acquired by another token.

        Raises:
            Exception: Any exception can be raised if the lock cannot be acquired.
        """
        ...

    async def release(self, *, name: str, token: str) -> bool:
        """Release a lock.

        Args:
            name: The name of the lock.
            token: The token to release the lock.

        Returns:
            True if the lock was released, False otherwise.

        Raises:
            Exception: Any exception can be raised if the lock cannot be released.
        """
        ...

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired.

        Args:
            name: The name of the lock.

        Returns:
            True if the lock is acquired, False otherwise.

        Raises:
            Exception: Any exception can be raised if the lock status cannot be checked.
        """
        ...

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned.

        Args:
            name: The name of the lock.
            token: The token to check.

        Returns:
            True if the lock is owned by the token, False otherwise.

        Raises:
            Exception: Any exception can be raised if the lock status cannot be checked.
        """
        ...


@runtime_checkable
class Synchronization(Protocol):
    """Synchronization Primitive Protocol."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the synchronization primitive."""
        ...


Seconds = PositiveFloat
