"""Resilience Backend Registries."""

from grelmicro._backends import DEFAULT_NAME, BackendRegistry
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    RateLimiterBackend,
)

rate_limiter_backend_registry: BackendRegistry[RateLimiterBackend] = (
    BackendRegistry(name="resilience")
)

circuit_breaker_backend_registry: BackendRegistry[CircuitBreakerBackend] = (
    BackendRegistry(name="resilience.circuitbreaker")
)


def get_rate_limiter_backend(name: str = DEFAULT_NAME) -> RateLimiterBackend:
    """Resolve a rate limiter backend by ``name``.

    Raises:
        BackendNotLoadedError: If no backend resolves.
    """
    return rate_limiter_backend_registry.get(name)


def get_circuit_breaker_backend(
    name: str = DEFAULT_NAME,
) -> CircuitBreakerBackend:
    """Resolve a circuit breaker backend by ``name``.

    Raises:
        BackendNotLoadedError: If no backend resolves.
    """
    return circuit_breaker_backend_registry.get(name)
