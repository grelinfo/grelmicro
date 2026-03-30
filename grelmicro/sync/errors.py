"""Synchronization Errors."""

import warnings

from grelmicro._backends import BackendNotLoadedError
from grelmicro.errors import GrelmicroError, SettingsValidationError

_TOKEN_DEPRECATION_MSG = (
    "The 'token' parameter is deprecated. "  # noqa: S105
    "Remove it from your code. Will be removed in 0.7.0."
)


def _warn_deprecated_token(token: str | None) -> None:
    """Emit a deprecation warning if the token parameter is passed."""
    if token is not None:
        warnings.warn(_TOKEN_DEPRECATION_MSG, DeprecationWarning, stacklevel=3)


__all__ = [
    "BackendNotLoadedError",
    "LockAcquireError",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockReentrantError",
    "LockReleaseError",
    "SyncBackendError",
    "SyncError",
    "SyncSettingsValidationError",
]


class SyncError(GrelmicroError):
    """Synchronization Primitive Error.

    This is the base class for all synchronization errors.
    """


class LockReentrantError(SyncError):
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


class SyncBackendError(SyncError):
    """Synchronization Backend Error."""


class LockLockedCheckError(SyncBackendError):
    """Lock Locked Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is acquired.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is acquired: name={name}")


class LockOwnedCheckError(SyncBackendError):
    """Lock Owned Check Error.

    This error is raised when an error on backend side occurs while checking if a lock is owned.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"Failed to check if lock is owned: name={name}")


class LockAcquireError(SyncBackendError):
    """Acquire Lock Error.

    This error is raised when an error on backend side occurs during lock acquisition.
    """

    def __init__(self, *, name: str, token: str | None = None) -> None:
        """Initialize the error."""
        _warn_deprecated_token(token)
        super().__init__(f"Failed to acquire lock: name={name}")


class LockReleaseError(SyncBackendError):
    """Lock Release Error.

    This error is raised when an error on backend side occurs during lock release.
    """

    def __init__(
        self, *, name: str, reason: str | None = None, token: str | None = None
    ) -> None:
        """Initialize the error."""
        _warn_deprecated_token(token)
        super().__init__(
            f"Failed to release lock: name={name}"
            + (f", reason={reason}" if reason else ""),
        )


class LockNotOwnedError(LockReleaseError):
    """Lock Not Owned Error during Release.

    This error is raised when an attempt is made to release a lock that is not owned, respectively
    the token is different or the lock is already expired.
    """

    def __init__(self, *, name: str, token: str | None = None) -> None:
        """Initialize the error."""
        _warn_deprecated_token(token)
        super().__init__(name=name, reason="lock not owned")


class SyncSettingsValidationError(SyncError, SettingsValidationError):
    """Synchronization Settings Validation Error."""
