"""Resilience Errors."""

from datetime import datetime

from grelmicro.errors import GrelmicroError, SettingsValidationError


class ResilienceError(GrelmicroError):
    """Base class for all resilience-related errors.

    This class serves as the base for all errors related to resilience mechanisms
    such as circuit breakers, retries, etc.
    """


class ResilienceSettingsValidationError(
    ResilienceError, SettingsValidationError
):
    """Resilience Settings Validation Error."""


class BulkheadFullError(ResilienceError):
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


class RateLimitExceededError(ResilienceError):
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


class CircuitBreakerError(ResilienceError):
    """Circuit breaker error.

    Raised when calls are not permitted by the circuit breaker.
    """

    def __init__(
        self,
        *,
        name: str,
        last_error_time: datetime | None = None,
        last_error: Exception | None = None,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.last_error = last_error
        self.last_error_time = last_error_time
        super().__init__(f"Circuit breaker '{name}' call not permitted")
