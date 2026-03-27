"""Cache Backend Registry."""

from grelmicro._backends import BackendRegistry
from grelmicro.cache._protocol import CacheBackend

cache_backend_registry: BackendRegistry[CacheBackend] = BackendRegistry(
    name="cache"
)


def get_cache_backend() -> CacheBackend:
    """Get the default cache backend.

    Raises:
        BackendNotLoadedError: If no cache backend has been registered.
    """
    return cache_backend_registry.get()
