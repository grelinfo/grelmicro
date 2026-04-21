"""Resilience."""

import warnings

from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import GCRA, Algorithm, TokenBucket
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
from grelmicro.resilience.memory import MemoryTokenBucket
from grelmicro.resilience.ratelimiter import RateLimiter, RateLimiterConfig

__all__ = [
    "GCRA",
    "Algorithm",
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ErrorDetails",
    "MemoryTokenBucket",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterConfig",
    "RateLimiterStrategy",
    "ResilienceError",
    "ResilienceSettingsValidationError",
    "TokenBucket",
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
