"""Rate Limiter Backend Registry."""

from grelmicro._backends import BackendRegistry
from grelmicro.resilience._protocol import RateLimiterBackend

rate_limiter_backend_registry: BackendRegistry[RateLimiterBackend] = (
    BackendRegistry(name="rate_limiter")
)


def get_rate_limiter_backend() -> RateLimiterBackend:
    """Get the default rate limiter backend.

    Raises:
        BackendNotLoadedError: If no rate limiter backend has been registered.
    """
    return rate_limiter_backend_registry.get()
