# Architecture

This section documents the internal design decisions and guarantees of grelmicro.

- **[Concurrency runtime](asyncio.md)**: Why grelmicro targets asyncio directly and not Trio or AnyIO.
- **[Backend Registry](backends.md)**: Shared registry pattern for swappable backends.
- **[Configuration](config.md)**: Explicit construction paths, `from_config(...)`, optional env resolution where it fits, and the library-not-app boundary.
- **[Live reconfiguration](reconfigure.md)**: Atomic config swap on a live component, the `Reconfigurable` mixin, and reader safety.
- **[Import Strategy](imports.md)**: Why backends are imported from submodules, not re-exported.
- **[Plugins](plugins.md)**: Entry-point groups that let third-party packages register Providers and Adapters.
- **[Multiple apps](multiple-apps.md)**: When two `Grelmicro` apps can run concurrently, and why `Log` and `Trace` are the exception.
- **[Synchronization](sync.md)**: Worker identity, token generation, lock design, and cleanup strategy.
- **[Kubernetes Backend](kubernetes.md)**: Lease resources, optimistic concurrency, and name sanitization.
- **[SQLite Backend](sqlite.md)**: WAL mode.
- **[Tracing](tracing.md)**: Context stack, concurrency safety, and decoupled layering.
- **[Testing](testing.md)**: `micro.override(...)` block and pytest conftest recipe.
