"""Resilience."""

import warnings

from grelmicro.resilience._protocol import RateLimiterBackend, RateLimitResult
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerMetrics,
    CircuitBreakerState,
    ErrorDetails,
)
from grelmicro.resilience.errors import (
    CircuitBreakerError,
    RateLimitExceededError,
    ResilienceError,
    ResilienceSettingsValidationError,
)
from grelmicro.resilience.ratelimiter import RateLimiter, RateLimiterConfig

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ErrorDetails",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterConfig",
    "ResilienceError",
    "ResilienceSettingsValidationError",
]


def __getattr__(name: str) -> type:
    if name == "ResilienceException":
        warnings.warn(
            "ResilienceException is deprecated, use ResilienceError instead. "
            "Will be removed in 0.7.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        globals()["ResilienceException"] = ResilienceError
        return ResilienceError
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
