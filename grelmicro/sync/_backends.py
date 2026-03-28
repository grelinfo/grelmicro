"""Synchronization Backend Registry."""

from grelmicro._backends import BackendRegistry
from grelmicro.sync.abc import SyncBackend

sync_backend_registry: BackendRegistry[SyncBackend] = BackendRegistry(
    name="lock"
)


def get_sync_backend() -> SyncBackend:
    """Get the default sync backend.

    Raises:
        BackendNotLoadedError: If no lock backend has been registered.
    """
    return sync_backend_registry.get()
