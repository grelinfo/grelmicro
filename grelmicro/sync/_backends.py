"""Grelmicro Backend Registry.

Contains loaded backends of each type to be used as default.

Note:
    For now, only lock backends are supported, but other backends may be added in the future.
"""

from typing import Literal, NotRequired, TypedDict

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import BackendNotLoadedError


class LoadedBackendsDict(TypedDict):
    """Loaded backends type."""

    lock: NotRequired[SyncBackend]


loaded_backends: LoadedBackendsDict = {}


def get_sync_backend() -> SyncBackend:
    """Get the lock backend."""
    backend: Literal["lock"] = "lock"
    try:
        return loaded_backends[backend]
    except KeyError:
        raise BackendNotLoadedError(backend) from None
