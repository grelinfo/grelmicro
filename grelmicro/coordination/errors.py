"""Coordination Errors."""

from grelmicro.errors import (
    GrelmicroError,
    LockTimeoutError,
    OutOfContextError,
    WouldBlockError,
)

__all__ = [
    "CoordinationBackendError",
    "CoordinationError",
    "LockAcquireError",
    "LockBackendError",
    "LockExtendError",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockReentrantError",
    "LockReleaseError",
    "LockTimeoutError",
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


_BACKEND_HINT = "Check the backend is open and reachable, then retry."
"""Fix named by a lock error whose backend call failed."""

_BACKEND_CLOSED_HINT = (
    "The backend is not open. Register it in Grelmicro(uses=[...]) so the "
    "app opens it, and use the lock while the app is open."
)
"""Fix named when the failed backend call found the backend not open."""


class _BackendCallError(LockBackendError):
    """A lock error raised when a call to the backend failed.

    The message ends with the fix. When the chained cause is an
    `OutOfContextError`, the backend was never opened or already closed, so
    the fix is opening it. Any other cause names checking the backend and
    retrying.
    """

    def __str__(self) -> str:
        """Return the failed action followed by the fix its cause calls for."""
        closed = isinstance(self.__cause__, OutOfContextError)
        hint = _BACKEND_CLOSED_HINT if closed else _BACKEND_HINT
        return f"{self.args[0]} {hint}"


class LockLockedCheckError(_BackendCallError):
    """Lock Locked Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is acquired.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is acquired: name={name}.")


class LockOwnedCheckError(_BackendCallError):
    """Lock Owned Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is owned.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is owned: name={name}.")


class LockAcquireError(_BackendCallError):
    """Acquire Lock Error.

    This error is raised when an error on backend side occurs during lock acquisition.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to acquire lock: name={name}.")


class LockExtendError(_BackendCallError):
    """Lock Extend Error.

    This error is raised when an error on backend side occurs while extending a held lease.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to extend lock: name={name}.")


class LockReleaseError(_BackendCallError):
    """Lock Release Error.

    This error is raised when an error on backend side occurs during lock release.
    """

    def __init__(self, *, name: str, reason: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(
            f"Failed to release lock: name={name}"
            + (f", reason={reason}" if reason else "")
            + ".",
        )


class LockNotOwnedError(CoordinationError):
    """Raised when a lock is used by a caller that does not hold it.

    Releasing, extending, or checking a guard all raise it when
    the caller never acquired the lock, already released it, or let its
    lease run out.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Lock not held: name={name}. This caller never acquired it, "
            f"already released it, or its lease ran out because the work "
            f"outran lease_duration=. Use the lock only while it is held, "
            f"and raise lease_duration= above how long the work runs.",
        )
