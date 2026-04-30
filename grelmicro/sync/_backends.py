"""Synchronization Backend Registry."""

from grelmicro._backends import DEFAULT_NAME, BackendRegistry
from grelmicro.sync.abc import SyncBackend

sync_backend_registry: BackendRegistry[SyncBackend] = BackendRegistry(
    name="sync"
)


def get_sync_backend(name: str = DEFAULT_NAME) -> SyncBackend:
    """Resolve a sync backend by ``name``.

    Raises:
        BackendNotLoadedError: If no backend resolves.
    """
    return sync_backend_registry.get(name)
