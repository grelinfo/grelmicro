"""Idempotency Errors."""

from grelmicro.errors import GrelmicroError


class IdempotencyError(GrelmicroError):
    """Base idempotency error."""


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
