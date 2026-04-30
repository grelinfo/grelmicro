"""Cache Backend Registry."""

from grelmicro._backends import DEFAULT_NAME, BackendRegistry
from grelmicro.cache._protocol import CacheBackend

cache_backend_registry: BackendRegistry[CacheBackend] = BackendRegistry(
    name="cache"
)


def get_cache_backend(name: str = DEFAULT_NAME) -> CacheBackend:
    """Resolve a cache backend by ``name``.

    Raises:
        BackendNotLoadedError: If no backend resolves.
    """
    return cache_backend_registry.get(name)
