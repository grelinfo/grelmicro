"""Synchronization."""

import warnings

from grelmicro.sync.abc import SyncPrimitive
from grelmicro.sync.errors import SyncError, SyncSettingsValidationError
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock

__all__ = [
    "LeaderElection",
    "Lock",
    "SyncError",
    "SyncPrimitive",
    "SyncSettingsValidationError",
    "TaskLock",
]


def __getattr__(name: str) -> type:
    if name == "Synchronization":
        warnings.warn(
            "Synchronization is deprecated, use SyncPrimitive instead. "
            "Will be removed in 0.7.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        globals()["Synchronization"] = SyncPrimitive
        return SyncPrimitive
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
