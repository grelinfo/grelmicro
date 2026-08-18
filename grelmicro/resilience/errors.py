"""Resilience Errors."""

from datetime import datetime

from grelmicro.errors import (
    AdmissionError,
    GrelmicroError,
)


class ResilienceError(GrelmicroError):
    """Base class for all resilience-related errors.

    This class serves as the base for all errors related to resilience mechanisms
    such as circuit breakers, retries, etc.
    """


class BulkheadFullError(ResilienceError, AdmissionError):
    """Bulkhead full error.

    Raised when a bulkhead has no free permit and the caller's
    `max_wait` elapsed (or was zero, the fail-fast default).
    """

    def __init__(
        self,
        *,
        name: str,
        max_concurrent: int,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.max_concurrent = max_concurrent
        super().__init__(
            f"Bulkhead '{name}' is full ({max_concurrent} concurrent calls)"
        )


class RateLimitExceededError(ResilienceError, AdmissionError):
    """Rate limit exceeded error.

    Raised when a rate limit check fails (too many requests).
    """

    def __init__(
        self,
        *,
        key: str,
        retry_after: float,
    ) -> None:
        """Initialize the error."""
        self.key = key
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded for key '{key}',"
            f" retry after {retry_after:.1f}s"
        )


class CircuitBreakerError(ResilienceError, AdmissionError):
    """Circuit breaker error.

    Raised when calls are not permitted by the circuit breaker.
    """

    def __init__(
        self,
        *,
        name: str,
        last_error_time: datetime | None = None,
        last_error: Exception | None = None,
        retry_after: float = 0.0,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.last_error = last_error
        self.last_error_time = last_error_time
        self.retry_after = retry_after
        """Seconds until the breaker next admits a probe, 0.0 when unknown.

        Read from the backend that holds the state, so it counts down on
        that backend's clock rather than this process's. A breaker held
        `FORCED_OPEN` by an operator reports 0.0: nothing releases it but
        an explicit reset.
        """
        super().__init__(f"Circuit breaker '{name}' call not permitted")


class DeadlineExceededError(ResilienceError, TimeoutError):
    """Raised when a `Timeout` deadline elapses.

    Subclasses the builtin `TimeoutError`, so an `except TimeoutError`
    around the block still catches it. Catch this instead to tell a
    deadline grelmicro set apart from a socket or driver timeout raised
    underneath it.
    """

    def __init__(
        self,
        *,
        name: str,
        timeout: float,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.timeout = timeout
        super().__init__(f"Timeout '{name}' exceeded its {timeout}s deadline")
