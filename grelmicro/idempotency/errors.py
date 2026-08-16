"""Idempotency Errors."""

from grelmicro.errors import GrelmicroError


class IdempotencyError(GrelmicroError):
    """Base idempotency error."""


class IdempotencyKeyMakerError(IdempotencyError, ValueError):
    """Raised when a `key_maker` returns a key that cannot separate callers.

    A key that is partly missing does not fail, it merges. Callers whose key
    lost the same component share one entry and can replay each other's
    response, while the request still answers normally. The middleware
    refuses the key instead of widening the boundary silently.
    """


class IdempotencyStateError(IdempotencyError, RuntimeError):
    """Raised when an `Operation` value is read in the wrong state.

    `Operation.result()` returns the stored response and is valid only
    on a replay. Calling it on a first execution, before a response is
    stored, raises this error. Guard the call with `if op.replayed:`.
    """


class IdempotencyWaitTimeoutError(IdempotencyError, TimeoutError):
    """Raised when a duplicate waits past `wait_timeout` for the first execution.

    Subclasses `TimeoutError`, so an `except TimeoutError` around the
    block catches it. Catch this instead to tell a single-flight wait
    apart from a backend that timed out inside the block.
    """

    def __init__(self, *, name: str, key: str, timeout: float) -> None:
        """Initialize the error."""
        self.name = name
        self.key = key
        self.timeout = timeout
        super().__init__(
            f"Idempotency({name!r}) waited {timeout}s for an execution "
            f"already in flight: key={key!r}"
        )


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
