"""Synchronization."""

import warnings
from typing import Annotated

from typing_extensions import Doc

from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.abc import SyncBackend, SyncPrimitive
from grelmicro.sync.errors import SyncError, SyncSettingsValidationError
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock


def use_backend(
    backend: Annotated[
        SyncBackend,
        Doc("The synchronization backend to register as the default."),
    ],
) -> None:
    """Register `backend` as the default synchronization backend.

    Idempotent: re-registering the same instance is a no-op.
    Registering a different instance warns and replaces.
    """
    sync_backend_registry.register(backend)


__all__ = [
    "LeaderElection",
    "Lock",
    "SyncError",
    "SyncPrimitive",
    "SyncSettingsValidationError",
    "TaskLock",
    "use_backend",
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
