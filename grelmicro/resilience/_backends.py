"""Rate Limiter Backend Registry."""

from grelmicro._backends import DEFAULT_NAME, BackendRegistry
from grelmicro.resilience._protocol import RateLimiterBackend

rate_limiter_backend_registry: BackendRegistry[RateLimiterBackend] = (
    BackendRegistry(name="resilience")
)


def get_rate_limiter_backend(name: str = DEFAULT_NAME) -> RateLimiterBackend:
    """Resolve a rate limiter backend by ``name``.

    Raises:
        BackendNotLoadedError: If no backend resolves.
    """
    return rate_limiter_backend_registry.get(name)
