# Coordination

- **Start here**: [Coordination guide](../coordination/index.md)
- **Common recipes**: [`Lock`](../coordination/lock.md), [`ReadWriteLock`](../coordination/read-write-lock.md), [`TaskLock`](../coordination/task-lock.md), [`LeaderElection`](../coordination/leader-election.md)
- **Backends**: [backend selection](../coordination/index.md#backends)

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
