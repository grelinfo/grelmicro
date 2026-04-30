"""Resilience."""

import warnings
from typing import Annotated

from typing_extensions import Doc

from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import (
    GCRAConfig,
    RateLimiterConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
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
from grelmicro.resilience.ratelimiter import RateLimiter


def use_backend(
    backend: Annotated[
        RateLimiterBackend,
        Doc("The rate limiter backend to register as the default."),
    ],
) -> None:
    """Register `backend` as the default rate limiter backend.

    Idempotent: re-registering the same instance is a no-op.
    Registering a different instance warns and replaces.
    """
    rate_limiter_backend_registry.register(backend)


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ErrorDetails",
    "GCRAConfig",
    "MemoryTokenBucket",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterConfig",
    "RateLimiterStrategy",
    "ResilienceError",
    "ResilienceSettingsValidationError",
    "TokenBucketConfig",
    "use_backend",
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
