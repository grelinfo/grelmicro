# Coordination

- **Start here**: [Coordination guide](../coordination.md)
- **Common recipes**: [`Lock`](../coordination.md#lock), [`ReadWriteLock`](../coordination.md#read-write-lock), [`TaskLock`](../coordination.md#task-lock), [`LeaderElection`](../coordination.md#leader-election)
- **Backends**: [backend selection](../coordination.md#backends)

::: grelmicro.coordination
    options:
      show_submodules: true
      members:
        - Coordination
        - Lock
        - ReadWriteLock
        - ReadWriteLockConfig
        - ReadGuard
        - WriteGuard
        - TaskLock
        - LeaderElection
        - LeaderElectionConfig
        - LeaderElectionBackend
        - LockBackend
        - ReadWriteLockBackend
        - ReadWriteLockState
        - WriteGrant
        - LeaderRecord
        - CoordinationError
