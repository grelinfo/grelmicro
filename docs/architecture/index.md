# Architecture

This section documents the internal design decisions and guarantees of grelmicro.

- **[Backend Registry](backends.md)**: Shared registry pattern for swappable backends.
- **[Import Strategy](imports.md)**: Why backends are imported from submodules, not re-exported.
- **[Synchronization](sync.md)**: Worker identity, token generation, lock design, and cleanup strategy.
- **[Kubernetes Backend](kubernetes.md)**: Lease resources, optimistic concurrency, and name sanitization.
- **[SQLite Backend](sqlite.md)**: WAL mode.
- **[Tracing](tracing.md)**: Context stack, concurrency safety, and decoupled layering.
