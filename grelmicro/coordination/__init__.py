"""Coordination primitives for distributed locking and leader election."""

from grelmicro.coordination._component import Coordination
from grelmicro.coordination._handle import LockHandle
from grelmicro.coordination.abc import (
    LeaderElectionBackend,
    LeaderRecord,
    LockBackend,
    LockPrimitive,
    ScheduleBackend,
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
    WouldBlockError,
)
from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.tasklock import TaskLock

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
    "LockHandle",
    "LockLockedCheckError",
    "LockNotOwnedError",
    "LockOwnedCheckError",
    "LockPrimitive",
    "LockReentrantError",
    "LockReleaseError",
    "ScheduleBackend",
    "TaskLock",
    "WouldBlockError",
]
