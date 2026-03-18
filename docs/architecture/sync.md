# Synchronization Internals

This page documents the internal design of the [Synchronization Primitives](../sync.md).

## Worker Identity

By default, each synchronization primitive (`Lock`, `TaskLock`, `LeaderElection`) generates a unique **worker identity** at instantiation using `token_hex(4)` (8 random hex characters, 32 bits of entropy) when no explicit `worker` parameter is provided.

This provides uniqueness across:

- **Multiple processes** (e.g., `uvicorn --workers N`): Each worker process imports the application independently, so `token_hex(4)` is called separately per process with independent randomness.
- **Multiple instances** within the same process: Each `Lock(...)` or `TaskLock(...)` call generates its own `token_hex(4)`, producing a different worker identity.

!!! warning "Pre-fork servers"
    If the ASGI server uses a pre-fork model (forking after the application is loaded), worker identities generated before the fork will be duplicated across child processes. Uvicorn does **not** pre-fork. It spawns workers via `subprocess.Popen`, so each worker imports the application independently. If using a pre-fork server, pass an explicit `worker` identity to avoid collisions.

## Token Generation

Lock tokens identify **who** holds a lock. They are derived deterministically from the worker identity and the current execution context using simple string concatenation:

| Primitive | Token | Scope |
|---|---|---|
| `Lock` | `{worker}:task:{task_id}` | Per async task |
| `Lock.from_thread` | `{worker}:thread:{thread_id}` | Per thread |
| `TaskLock` | `{worker}:task:{task_id}` | Per async task |
| `TaskLock.from_thread` | `{worker}:thread:{thread_id}` | Per thread |
| `LeaderElection` | `worker` directly | Per process |

This design provides the following guarantees:

- **Mutual exclusion**: Different async tasks or threads produce different tokens for the same lock, ensuring only one caller holds the lock at a time.
- **Re-entrancy**: The same async task (or thread) always produces the same deterministic token, allowing it to re-acquire the lock to extend the lease.
- **Isolation**: Different lock instances have different worker identities, so their tokens never collide even when used from the same task or thread.

## Lock Name and Backend Key

Each synchronization primitive automatically prefixes the user-provided `name` with a type-specific namespace to form the backend key:

| Primitive | Name | Backend Key |
|---|---|---|
| `Lock("my-resource")` | `my-resource` | `lock:my-resource` |
| `TaskLock("cleanup")` | `cleanup` | `tasklock:cleanup` |
| `LeaderElection("main")` | `main` | `leader:main` |

This prevents accidental collisions between different primitive types sharing the same backend. A `Lock("x")` and a `TaskLock("x")` operate on independent backend entries.

!!! warning "Breaking change"
    Prior versions used the `name` parameter directly as the backend key without any prefix. After upgrading, existing locks stored in backends (Redis, PostgreSQL) will no longer match. Ensure all running instances are upgraded together so they use the same key format.

## Lock Cleanup

Expired locks are never actively removed during normal operation. Instead, all backends use a **lazy filtering** strategy combined with **cleanup on exit**:

1. **Lazy filtering**: Every `locked()`, `owned()`, and `acquire()` call includes an expiry check (`expire_at >= now`), so expired locks are simply ignored without requiring deletion.
2. **Cleanup on exit**: When the backend context manager exits (`__aexit__`), all expired locks are deleted in bulk. This keeps storage clean across graceful restarts.

If the process crashes without exiting the context manager, expired locks remain in storage but are harmless — they will be filtered out by all subsequent operations and cleaned up on the next graceful shutdown.

### Backend-specific cleanup

- **SQLite / PostgreSQL**: A single bulk `DELETE ... WHERE expire_at < now` removes all stale rows before closing the connection.
- **Kubernetes**: Lists all Lease resources labeled `app.kubernetes.io/managed-by: grelmicro` and deletes each expired lease individually. The Kubernetes API does not support bulk conditional deletion. `NOT_FOUND` errors are silently ignored to handle concurrent deletions.
