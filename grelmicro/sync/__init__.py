"""Synchronization."""

import warnings
from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.abc import SyncBackend, SyncPrimitive
from grelmicro.sync.errors import SyncError, SyncSettingsValidationError
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock


def register(
    backend: Annotated[SyncBackend, Doc("The synchronization backend.")],
    name: Annotated[
        str, Doc("Name to register the backend under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register ``backend`` under ``name`` (defaults to ``"default"``)."""
    sync_backend_registry.register(backend, name)


def unregister(
    name: Annotated[
        str, Doc("Name of the registered backend to remove.")
    ] = DEFAULT_NAME,
    backend: Annotated[
        SyncBackend | None,
        Doc("Optional backend instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered backend under ``name``."""
    sync_backend_registry.unregister(name, backend)


def use_backend(
    backend: Annotated[
        SyncBackend,
        Doc("The synchronization backend to register as the default."),
    ],
) -> None:
    """Register ``backend`` under the ``"default"`` name."""
    sync_backend_registry.register(backend, DEFAULT_NAME)


def use(
    backend: Annotated[
        SyncBackend | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: SyncBackend,
) -> AbstractContextManager[None]:
    """Install task-scoped backend overrides.

    Use as a context manager:

        with sync.use(MemorySyncBackend()):
            ...
    """
    return sync_backend_registry.use(backend, **named)


__all__ = [
    "LeaderElection",
    "Lock",
    "SyncError",
    "SyncPrimitive",
    "SyncSettingsValidationError",
    "TaskLock",
    "register",
    "unregister",
    "use",
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
