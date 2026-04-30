"""Resilience."""

import warnings
from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
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


def register(
    name: Annotated[str, Doc("Name to register the backend under.")],
    backend: Annotated[RateLimiterBackend, Doc("The rate limiter backend.")],
) -> None:
    """Register ``backend`` under ``name``."""
    rate_limiter_backend_registry.register(name, backend)


def unregister(
    name: Annotated[str, Doc("Name of the registered backend to remove.")],
    backend: Annotated[
        RateLimiterBackend | None,
        Doc("Optional backend instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered backend under ``name``."""
    rate_limiter_backend_registry.unregister(name, backend)


def use_backend(
    backend: Annotated[
        RateLimiterBackend,
        Doc("The rate limiter backend to register as the default."),
    ],
) -> None:
    """Register ``backend`` under the ``"default"`` name."""
    rate_limiter_backend_registry.register(DEFAULT_NAME, backend)


def use(
    backend: Annotated[
        RateLimiterBackend | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: RateLimiterBackend,
) -> AbstractContextManager[None]:
    """Install task-scoped backend overrides."""
    return rate_limiter_backend_registry.use(backend, **named)


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
    "register",
    "unregister",
    "use",
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
