# Synchronization

- **Start here**: [Synchronization Primitives guide](../sync.md)
- **Common recipes**: [`Lock`](../sync.md#lock), [`TaskLock`](../sync.md#task-lock), [`LeaderElection`](../sync.md#leader-election)
- **Configuration**: [Backend selection](../sync.md#backend), [environment variables](../sync.md#environment-variables)

::: grelmicro.sync
    options:
      show_submodules: true
      members:
        - LeaderElection
        - Lock
        - SyncError
        - SyncPrimitive
        - SyncSettingsValidationError
        - TaskLock
