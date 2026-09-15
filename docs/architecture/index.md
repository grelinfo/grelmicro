# Architecture

This section documents the internal design decisions and guarantees of grelmicro.

- **[Concurrency runtime](asyncio.md)**: Why grelmicro targets asyncio directly and not Trio or AnyIO.
- **[Backends and Adapters](backends.md)**: How Providers, Components, Backends, and Adapters fit together.
- **[Configuration](config.md)**: Explicit construction paths, `from_config(...)`, optional env resolution where it fits, and the library-not-app boundary.
- **[Live reconfiguration](reconfigure.md)**: Atomic config swap on a live component, the `Reconfigurable` mixin, and reader safety.
- **[HTTP components](http.md)**: The contract the HTTP components follow, and why it differs from the resilience patterns.
- **[JWT verification](jwt.md)**: The compiled core, the crypto provider, the cache, and every measurement behind them.
- **[Outbound tokens](oauth.md)**: Two grants as two patterns, which audience an assertion names, and why the caller's token is passed rather than read from the request.
- **[Rust](rust.md)**: Which hot paths are compiled, and the measured line that decides it.
- **[Import Strategy](imports.md)**: Why backends are imported from submodules, not re-exported.
- **[Plugins](plugins.md)**: Entry-point groups that let third-party packages register Providers and Adapters.
- **[Multiple apps](multiple-apps.md)**: When two `Grelmicro` apps can run concurrently, and why `Log`, `Trace`, and `Metrics` are the exception.
- **[Decorators](decorators.md)**: Which decorators take the bare `@deco` form, which require `@deco(...)`, and which wrap sync functions.
- **[API Conventions](api-conventions.md)**: Constructor and factory rules: positional `name` on patterns, keyword-only `name` on components, factory classmethods for algorithms.
- **[Sync from thread](sync-from-thread.md)**: How a synchronous handler calls an async primitive, and why the entry point is explicit.
- **[Coordination](coordination.md)**: Worker identity, token generation, lock design, and cleanup strategy.
- **[Outbox](outbox.md)**: The dual-write problem, the staging table, and how the relay delivers at least once.
- **[Graceful shutdown](graceful-shutdown.md)**: What happens between `SIGTERM` and `SIGKILL`, and how each component drains.
- **[Kubernetes Backend](kubernetes.md)**: Lease resources, optimistic concurrency, and name sanitization.
- **[SQLite Backend](sqlite.md)**: WAL mode.
- **[Tracing](tracing.md)**: Context stack, concurrency safety, and decoupled layering.
- **[Testing](testing.md)**: `micro.override(...)` block and pytest conftest recipe.
