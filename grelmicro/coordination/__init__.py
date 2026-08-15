"""Coordination primitives for distributed locking and leader election."""

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
    CoordinationSettingsValidationError,
    LockAcquireError,
    LockBackendError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
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
    "CoordinationSettingsValidationError",
    "LeaderElection",
    "LeaderElectionBackend",
    "LeaderElectionConfig",
    "LeaderRecord",
    "Lock",
    "LockAcquireError",
    "LockBackend",
    "LockBackendError",
    "LockConfig",
    "LockHandle",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockPrimitive",
    "LockReentrantError",
    "LockReleaseError",
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
