"""Coordination primitives for distributed locking and leader election.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.coordination._component import Coordination
    from grelmicro.coordination._guards import ReadGuard, WriteGuard
    from grelmicro.coordination._handle import LockHandle
    from grelmicro.coordination._protocol import (
        LeaderElectionBackend,
        LeaderRecord,
        LockBackend,
        LockPrimitive,
        ReadWriteLockBackend,
        ReadWriteLockState,
        ScheduleBackend,
        WriteGrant,
    )
    from grelmicro.coordination.errors import (
        CoordinationBackendError,
        CoordinationError,
        LockAcquireError,
        LockBackendError,
        LockExtendError,
        LockLockedCheckError,
        LockNotOwnedError,
        LockOwnedCheckError,
        LockReentrantError,
        LockReleaseError,
        LockTimeoutError,
        LockUpgradeError,
        WouldBlockError,
    )
    from grelmicro.coordination.leaderelection import (
        LeaderElection,
        LeaderElectionConfig,
    )
    from grelmicro.coordination.lock import Lock, LockConfig
    from grelmicro.coordination.readwritelock import (
        ReadWriteLock,
        ReadWriteLockConfig,
    )
    from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig

__all__ = [
    "Coordination",
    "CoordinationBackendError",
    "CoordinationError",
    "LeaderElection",
    "LeaderElectionBackend",
    "LeaderElectionConfig",
    "LeaderRecord",
    "Lock",
    "LockAcquireError",
    "LockBackend",
    "LockBackendError",
    "LockConfig",
    "LockExtendError",
    "LockHandle",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockPrimitive",
    "LockReentrantError",
    "LockReleaseError",
    "LockTimeoutError",
    "LockUpgradeError",
    "ReadGuard",
    "ReadWriteLock",
    "ReadWriteLockBackend",
    "ReadWriteLockConfig",
    "ReadWriteLockState",
    "ScheduleBackend",
    "TaskLock",
    "TaskLockConfig",
    "WouldBlockError",
    "WriteGrant",
    "WriteGuard",
]

_LAZY: dict[str, str] = {
    "Coordination": "grelmicro.coordination._component",
    "CoordinationBackendError": "grelmicro.coordination.errors",
    "CoordinationError": "grelmicro.coordination.errors",
    "LeaderElection": "grelmicro.coordination.leaderelection",
    "LeaderElectionBackend": "grelmicro.coordination._protocol",
    "LeaderElectionConfig": "grelmicro.coordination.leaderelection",
    "LeaderRecord": "grelmicro.coordination._protocol",
    "Lock": "grelmicro.coordination.lock",
    "LockAcquireError": "grelmicro.coordination.errors",
    "LockBackend": "grelmicro.coordination._protocol",
    "LockBackendError": "grelmicro.coordination.errors",
    "LockConfig": "grelmicro.coordination.lock",
    "LockExtendError": "grelmicro.coordination.errors",
    "LockHandle": "grelmicro.coordination._handle",
    "LockLockedCheckError": "grelmicro.coordination.errors",
    "LockNotOwnedError": "grelmicro.coordination.errors",
    "LockOwnedCheckError": "grelmicro.coordination.errors",
    "LockPrimitive": "grelmicro.coordination._protocol",
    "LockReentrantError": "grelmicro.coordination.errors",
    "LockReleaseError": "grelmicro.coordination.errors",
    "LockTimeoutError": "grelmicro.coordination.errors",
    "LockUpgradeError": "grelmicro.coordination.errors",
    "ReadGuard": "grelmicro.coordination._guards",
    "ReadWriteLock": "grelmicro.coordination.readwritelock",
    "ReadWriteLockBackend": "grelmicro.coordination._protocol",
    "ReadWriteLockConfig": "grelmicro.coordination.readwritelock",
    "ReadWriteLockState": "grelmicro.coordination._protocol",
    "ScheduleBackend": "grelmicro.coordination._protocol",
    "TaskLock": "grelmicro.coordination.tasklock",
    "TaskLockConfig": "grelmicro.coordination.tasklock",
    "WouldBlockError": "grelmicro.coordination.errors",
    "WriteGrant": "grelmicro.coordination._protocol",
    "WriteGuard": "grelmicro.coordination._guards",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
