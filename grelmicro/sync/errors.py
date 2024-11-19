"""GrelMicro Synchronization Primitive Errors."""


class SyncError(Exception):
    """Synchronization Primitive Error.

    This the base class for all lock errors.
    """


class LockBackendError(SyncError):
    """Lock Backend Error."""


class LockAcquireError(LockBackendError):
    """Acquire Lock Error.

    This error is raised when an error on backend side occurs during lock acquisition.
    """

    def __init__(self, *, name: str, token: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to acquire lock: name={name}, token={token}")


class LockReleaseError(LockBackendError):
    """Lock Release Error.

    This error is raised when an error on backend side occurs during lock release.
    """

    def __init__(self, *, name: str, token: str, reason: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(
            f"Failed to release lock: name={name}, token={token}"
            + (f", reason={reason}" if reason else ""),
        )


class LockNotOwnedError(LockReleaseError):
    """Lock Not Owned Error during Release.

    This error is raised when an attempt is made to release a lock that is not owned, respectively
    the token is different or the lock is already expired.
    """

    def __init__(self, *, name: str, token: str) -> None:
        """Initialize the error."""
        super().__init__(name=name, token=token, reason="lock not owned")
