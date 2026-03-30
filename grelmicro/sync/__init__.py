"""Synchronization."""

import warnings

from grelmicro.sync.abc import SyncPrimitive
from grelmicro.sync.errors import SyncError, SyncSettingsValidationError
from grelmicro.sync.kubernetes import KubernetesSyncBackend
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.lock import Lock
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.postgres import PostgresSyncBackend
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.sync.sqlite import SQLiteSyncBackend
from grelmicro.sync.tasklock import TaskLock

__all__ = [
    "KubernetesSyncBackend",
    "LeaderElection",
    "Lock",
    "MemorySyncBackend",
    "PostgresSyncBackend",
    "RedisSyncBackend",
    "SQLiteSyncBackend",
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
