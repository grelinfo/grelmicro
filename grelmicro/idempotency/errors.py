"""Idempotency Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class IdempotencyError(GrelmicroError):
    """Base idempotency error."""


class IdempotencySettingsValidationError(
    IdempotencyError, SettingsValidationError
):
    """Idempotency Settings Validation Error."""


class IdempotencyStateError(IdempotencyError, RuntimeError):
    """Raised when an `Operation` value is read in the wrong state.

    `Operation.result()` returns the stored response and is valid only
    on a replay. Calling it on a first execution, before a response is
    stored, raises this error. Guard the call with `if op.replayed:`.
    """


class IdempotencyConflictError(IdempotencyError):
    """Raised when a key is replayed with a different payload fingerprint.

    The same idempotency key arrived twice with different payloads. The
    stored fingerprint from the first execution does not match the
    fingerprint supplied on the replay, so the second call is rejected
    instead of returning a response computed for a different request.
    """

    def __init__(
        self,
        *,
        name: str,
        key: str,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.key = key
        super().__init__(
            f"Idempotency key {key!r} on {name!r} replayed with a "
            f"different payload fingerprint"
        )
