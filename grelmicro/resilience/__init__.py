"""Resilience."""

import warnings

from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerMetrics,
    CircuitBreakerState,
    ErrorDetails,
)
from grelmicro.resilience.errors import CircuitBreakerError, ResilienceError

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ErrorDetails",
    "ResilienceError",
]


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
