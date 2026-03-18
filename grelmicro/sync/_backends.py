"""grelmicro Backend Registry.

Contains loaded backends of each type to be used as default.

Note:
    For now, only lock backends are supported, but other backends may be added in the future.
"""

from contextvars import ContextVar, Token

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import BackendNotLoadedError

_sync_backend_var: ContextVar[SyncBackend | None] = ContextVar(
    "sync_backend", default=None
)


_BACKEND_NAME = "lock"


def get_sync_backend() -> SyncBackend:
    """Get the lock backend."""
    backend = _sync_backend_var.get()
    if backend is None:
        raise BackendNotLoadedError(_BACKEND_NAME)
    return backend


def set_sync_backend(
    backend: SyncBackend | None,
) -> Token[SyncBackend | None]:
    """Set the sync backend for the current context. Returns a token to reset."""
    return _sync_backend_var.set(backend)


def reset_sync_backend(token: Token[SyncBackend | None]) -> None:
    """Reset the sync backend to its previous value."""
    _sync_backend_var.reset(token)
