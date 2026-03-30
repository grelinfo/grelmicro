"""Resilience Errors."""

import warnings
from datetime import datetime

from grelmicro.errors import GrelmicroError


class ResilienceError(GrelmicroError):
    """Base class for all resilience-related errors.

    This class serves as the base for all errors related to resilience mechanisms
    such as circuit breakers, retries, etc.
    """


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


def __getattr__(name: str) -> type:
    if name == "ResilienceException":
        warnings.warn(
            "ResilienceException is deprecated, use ResilienceError instead. "
            "Will be removed in 0.7.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return ResilienceError
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
