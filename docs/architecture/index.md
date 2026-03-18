# Architecture

This section documents the internal design decisions and guarantees of grelmicro.

- **[Synchronization](sync.md)**: Worker identity, token generation, lock design, and cleanup strategy.
- **[Kubernetes Backend](kubernetes.md)**: Lease resources, optimistic concurrency, and name sanitization.
- **[PostgreSQL Backend](postgres.md)**: Timestamp strategy and design decisions.
- **[SQLite Backend](sqlite.md)**: Timestamp strategy, WAL mode, and design decisions.
