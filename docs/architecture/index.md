# Architecture

This section documents the internal design decisions and guarantees of grelmicro.

- **[Backend Registry](backends.md)**: Shared registry pattern for swappable backends.
- **[Configuration](config.md)**: Explicit construction paths, `from_config(...)`, optional env resolution where it fits, and the library-not-app boundary.
- **[Configuration rollout plan](config-plan.md)**: How the configuration contract ships across components, component by component.
- **[Import Strategy](imports.md)**: Why backends are imported from submodules, not re-exported.
- **[Synchronization](sync.md)**: Worker identity, token generation, lock design, and cleanup strategy.
- **[Kubernetes Backend](kubernetes.md)**: Lease resources, optimistic concurrency, and name sanitization.
- **[SQLite Backend](sqlite.md)**: WAL mode.
- **[Tracing](tracing.md)**: Context stack, concurrency safety, and decoupled layering.
