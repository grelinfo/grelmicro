"""Coordination Errors."""

from grelmicro.errors import (
    GrelmicroError,
    WouldBlockError,
)

__all__ = [
    "CoordinationBackendError",
    "CoordinationError",
    "LockAcquireError",
    "LockBackendError",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockReentrantError",
    "LockReleaseError",
    "LockUpgradeError",
    "WouldBlockError",
]


class CoordinationError(GrelmicroError):
    """Coordination Primitive Error.

    This is the base class for all coordination errors.
    """


class CoordinationBackendError(CoordinationError):
    """Coordination Backend Error.

    Raised when a primitive is requested from a `Coordination` component that
    has no backend wired for that primitive.
    """


class LockReentrantError(CoordinationError):
    """Lock Reentrant Error.

    This error is raised when a lock that does not support nested usage
    is acquired while already held.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Lock does not support nested usage: name={name}."
            f" The lock is already acquired by this instance."
            f" Use separate instances if you need independent locks."
        )


class LockUpgradeError(CoordinationError):
    """Lock Upgrade Error.

    This error is raised when a task that holds a read lock asks for the
    write lock on the same `ReadWriteLock`. Two readers upgrading at once
    wait for each other forever, so the upgrade is refused. Take the write
    lock from the start when the body may write.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Read-write lock does not support upgrade: name={name}."
            f" This task holds the read lock and asked for the write lock."
            f" Acquire the write lock first when the body may write."
        )


class LockBackendError(CoordinationError):
    """Lock Backend Error."""


class LockLockedCheckError(LockBackendError):
    """Lock Locked Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is acquired.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is acquired: name={name}")


class LockOwnedCheckError(LockBackendError):
    """Lock Owned Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is owned.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is owned: name={name}")


class LockAcquireError(LockBackendError):
    """Acquire Lock Error.

    This error is raised when an error on backend side occurs during lock acquisition.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to acquire lock: name={name}")


class LockReleaseError(LockBackendError):
    """Lock Release Error.

    This error is raised when an error on backend side occurs during lock release.
    """

    def __init__(self, *, name: str, reason: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(
            f"Failed to release lock: name={name}"
            + (f", reason={reason}" if reason else ""),
        )


class LockNotOwnedError(LockReleaseError):
    """Lock Not Owned Error during Release.

    This error is raised when an attempt is made to release a lock that is not owned, respectively
    the token is different or the lock is already expired.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(name=name, reason="lock not owned")
