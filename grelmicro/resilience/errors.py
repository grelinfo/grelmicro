"""Resilience Errors."""

from datetime import datetime


class ResilienceException(Exception):  # noqa: N818
    """Base class for all resilience-related exceptions.

    This class serves as the base for all exceptions related to resilience mechanisms
    such as circuit breakers, retries, etc.
    """


class CircuitBreakerError(ResilienceException):
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
