# Coordination Internals

This page documents the internal design of the [Coordination primitives](../coordination.md).

## Worker Identity

By default, each coordination primitive (`Lock`, `TaskLock`, `LeaderElection`) generates a unique **worker identity** at instantiation using `token_hex(4)` (8 random hex characters, 32 bits of entropy) when no explicit `worker` parameter is provided.

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
- **Idempotent**: The same async task (or thread) always produces the same deterministic token, so the backend accepts a re-acquire and extends the lease.
- **Isolation**: Different lock instances have different worker identities, so their tokens never collide even when used from the same task or thread.

## Lock Name and Backend Key

Each coordination primitive automatically prefixes the user-provided `name` with a type-specific namespace to form the backend key:

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
2. **Cleanup on exit**: When the backend context manager exits (`__aexit__`), expired locks are vacated in place. This keeps storage clean across graceful restarts.

If the process crashes without exiting the context manager, expired locks remain in storage but are harmless: they will be filtered out by all subsequent operations and cleaned up on the next graceful shutdown.

### Backend-specific cleanup

- **SQLite / PostgreSQL**: A single bulk `UPDATE ... SET token = NULL, expire_at = NULL WHERE expire_at < now` clears all stale rows before closing the connection. The row is kept so the fence counter survives across restart cycles.
- **Kubernetes**: Lists all Lease resources labeled `app.kubernetes.io/managed-by: grelmicro` and vacates each expired lease in place by clearing `holderIdentity`, `acquireTime`, and `renewTime` via a REPLACE. The Lease object is never deleted, so `spec.leaseTransitions` survives across release and re-acquire cycles. `NOT_FOUND` errors are silently ignored.

## Fencing token guarantees

A fencing token is a strictly increasing integer that grelmicro mints on each
free-to-held transition. The token is per-name, per-backend, and survives
release and re-acquire cycles: it only grows, never repeats.

### Monotonicity by backend

| Backend | Monotonicity scope | Guarantee |
|---|---|---|
| Memory | Per adapter instance (process lifetime) | Strictly increasing per name within one process. The `_fences` counter persists for the adapter lifetime and is never reset, even across release cycles. |
| Redis | Per lock name, per Redis master | Strictly increasing per name against the master. A separate `fence:<name>` counter key is incremented atomically inside the acquire Lua script on every free-to-held transition and is never deleted. |
| PostgreSQL | Per lock name, per database | Strictly increasing per name within the database. A `fence BIGINT` column is incremented via `fence + 1` on every free-to-held transition. Release clears the holder but keeps the row and its fence value. |
| SQLite | Per lock name, per database file | Strictly increasing per name within one database file. The `fence INTEGER` column follows the same bump-on-transition pattern as PostgreSQL, inside a `BEGIN IMMEDIATE` transaction. |
| Kubernetes | Per lock name, per Kubernetes cluster | Strictly increasing per name. The `spec.leaseTransitions` counter is incremented on every free-to-held transition. Release vacates the holder in place but keeps the Lease object, so the counter survives across release and re-acquire cycles. |

### Resource-side fencing check

grelmicro mints and returns the token. The resource that you write to must check
it. The pattern: store the highest token accepted alongside the resource and
reject writes that arrive with a lower or equal token.

Example with a SQL database:

```sql
-- On the resource table:
ALTER TABLE orders ADD COLUMN lock_fence bigint NOT NULL DEFAULT 0;

-- Every write from the lock holder passes the fencing token:
UPDATE orders
SET    data = :data,
       lock_fence = :token
WHERE  id = :order_id
AND    lock_fence < :token;
```

If the `UPDATE` affects zero rows, the write was rejected. An old holder that
resumed after a partition cannot overwrite a newer holder's data.

Python side:

```python
async with Lock("order:{order_id}") as held:
    rows = await db.execute(
        "UPDATE orders SET data=:data, lock_fence=:token "
        "WHERE id=:id AND lock_fence < :token",
        {"data": payload, "id": order_id, "token": held.fencing_token},
    )
    if rows.rowcount == 0:
        raise RuntimeError("Write rejected: fencing token too low")
```
