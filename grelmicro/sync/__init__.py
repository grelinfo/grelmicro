"""Synchronization."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro._deprecation import warn_legacy
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync._component import Sync
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
    """Register ``backend`` under ``name`` (defaults to ``"default"``).

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[Sync(backend, name=name)])` instead.
    """
    warn_legacy(
        "grelmicro.sync.register",
        "`Grelmicro(uses=[Sync(backend, name=name)])`",
    )
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
    """Remove the registered backend under ``name``.

    Deprecated since 0.23.0, removed in 1.0.0. Construct a fresh `Grelmicro`
    app instead of mutating a shared registry.
    """
    warn_legacy(
        "grelmicro.sync.unregister",
        "a fresh `Grelmicro(uses=[...])`",
    )
    sync_backend_registry.unregister(name, backend)


def use_backend(
    backend: Annotated[
        SyncBackend,
        Doc("The synchronization backend to register as the default."),
    ],
) -> None:
    """Register ``backend`` under the ``"default"`` name.

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[Sync(backend)])` (or pass the backend directly,
    `Grelmicro(uses=[backend])` for first-party backends).
    """
    warn_legacy(
        "grelmicro.sync.use_backend",
        "`Grelmicro(uses=[Sync(backend)])`",
    )
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

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `async with micro.override(Sync(backend)):` instead.
    """
    warn_legacy(
        "grelmicro.sync.use",
        "`async with micro.override(Sync(backend)):`",
    )
    return sync_backend_registry.use(backend, **named)


__all__ = [
    "LeaderElection",
    "Lock",
    "Sync",
    "SyncError",
    "SyncPrimitive",
    "SyncSettingsValidationError",
    "TaskLock",
    "register",
    "unregister",
    "use",
    "use_backend",
]
