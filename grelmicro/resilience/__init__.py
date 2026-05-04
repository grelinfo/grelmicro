"""Resilience."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro.resilience._backends import (
    circuit_breaker_backend_registry,
    rate_limiter_backend_registry,
)
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
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
    backend: Annotated[RateLimiterBackend, Doc("The rate limiter backend.")],
    name: Annotated[
        str, Doc("Name to register the backend under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register ``backend`` under ``name`` (defaults to ``"default"``)."""
    rate_limiter_backend_registry.register(backend, name)


def unregister(
    name: Annotated[
        str, Doc("Name of the registered backend to remove.")
    ] = DEFAULT_NAME,
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
    rate_limiter_backend_registry.register(backend, DEFAULT_NAME)


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


def register_circuit_breaker(
    backend: Annotated[
        CircuitBreakerBackend, Doc("The circuit breaker backend.")
    ],
    name: Annotated[
        str, Doc("Name to register the backend under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register a circuit breaker ``backend`` under ``name``."""
    circuit_breaker_backend_registry.register(backend, name)


def unregister_circuit_breaker(
    name: Annotated[
        str, Doc("Name of the registered backend to remove.")
    ] = DEFAULT_NAME,
    backend: Annotated[
        CircuitBreakerBackend | None,
        Doc("Optional backend instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered circuit breaker backend under ``name``."""
    circuit_breaker_backend_registry.unregister(name, backend)


def use_circuit_breaker_backend(
    backend: Annotated[
        CircuitBreakerBackend,
        Doc("The circuit breaker backend to register as the default."),
    ],
) -> None:
    """Register a circuit breaker ``backend`` under the ``"default"`` name."""
    circuit_breaker_backend_registry.register(
        backend, DEFAULT_NAME
    )  # pragma: no cover


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerBackend",
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
    "register_circuit_breaker",
    "unregister",
    "unregister_circuit_breaker",
    "use",
    "use_backend",
    "use_circuit_breaker_backend",
]
