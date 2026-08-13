# Read-Write Lock

A [`Lock`](lock.md) lets one caller in at a time, readers included. A
`ReadWriteLock` lets every reader in at once and keeps writers alone. Reach for
it when a resource is read far more often than it is written: a catalog, a
routing table, a rendered report, a config blob in object storage.

```python
--8<-- "coordination/readwritelock.py"
```

`catalog.read` and `catalog.write` are two views of one lock. Each is a full
primitive with `acquire(timeout=...)`, `acquire_nowait()`, `extend()`,
`release()`, and a `from_thread` adapter, exactly like `Lock`.

## Guards

Entering a mode binds a guard. The guard is the only thing that carries the
proof you hold the lock, so a function that writes can demand one in its
signature and the type checker rejects a caller who never took the lock:

```python
--8<-- "coordination/readwritelock_guards.py"
```

`ReadGuard` and `WriteGuard` are different types. A function annotated
`guard: WriteGuard` cannot be called with a read guard. Reading
`guard.fencing_token` after the guard is released, or after its lease expired,
raises `LockNotOwnedError` instead of handing back a stale token.

| Guard | Carries | Use it for |
|---|---|---|
| `ReadGuard` | `generation`, `expires_in` | Detecting that a writer landed since you read. |
| `WriteGuard` | `fencing_token`, `poisoned`, `expires_in` | Fencing every write, and spotting a crashed predecessor. |

## Writers never starve

The lock is writer-preferring. A writer that finds readers in the way records an
intent, and new readers wait behind it. Readers already inside finish and the
writer goes in next. Without this, a steady stream of readers holds a writer out
forever.

The intent carries its own lease. A writer that dies while waiting stops holding
readers back as soon as that lease expires.

## Poison

A write that crashes halfway leaves the resource in whatever state it reached.
The next writer sees `poisoned` set to `True`, which says the previous holder's
lease expired without a release:

```python
--8<-- "coordination/readwritelock_downgrade.py"
```

`poisoned` is a fact, not a lock state. The write lock is yours either way, and
what a half-finished predecessor means is yours to decide.

## Downgrade, and why there is no upgrade

`await guard.downgrade()` turns a held write lock into a read lock with no gap in
between, so no other writer can slip in. Use it to publish a rebuilt value and
then keep reading it.

There is no upgrade. Two callers that both hold a read lock and both wait to
become the writer wait for each other forever. `ReadWriteLock` raises
`LockUpgradeError` rather than shipping a deadlock. Take the write lock from the
start when you might write.

## Backends

Every coordination backend implements it.

```python
--8<-- "coordination/readwritelock_redis.py"
```

| Backend | Holds readers in | Notes |
|---|---|---|
| Redis, Valkey | A sorted set of reader leases, updated in one server-side step | Fastest. On a cluster, the prefix needs a hash tag. |
| PostgreSQL | Reader rows, updated under an advisory lock | Tables are created on first connect. Pass `auto_migrate=False` to manage them yourself. |
| SQLite | Reader rows, updated in one write transaction | One host only. Lease durations round up to whole seconds. |
| Kubernetes | Annotations on the Lease that holds the writer | Coarse-grained. Every reader renewal writes to etcd, and annotation size caps readers in the hundreds. |
| Memory | A process-local dict | Tests and single-process apps. |

Every holder has its own lease, so a reader that died is dropped by the next
writer's acquire rather than blocking it until a shared expiry fires.

## Configuration

Same fields as [`Lock`](lock.md#configuration), read from
`GREL_READWRITELOCK_{NAME_UPPER}_*`. The default instance drops the name segment
and reads `GREL_READWRITELOCK_*`.

--8<-- "env_gate.md"

| Env var | Config field | Type | Default |
|---|---|---|---|
| `GREL_READWRITELOCK_{NAME_UPPER}_WORKER` | `worker` | `str \| UUID` | generated UUID |
| `GREL_READWRITELOCK_{NAME_UPPER}_LEASE_DURATION` | `lease_duration` | `float` (> 0) | `60` |
| `GREL_READWRITELOCK_{NAME_UPPER}_RETRY_INTERVAL` | `retry_interval` | `float` (>= 0.001) | `0.1` |
| `GREL_READWRITELOCK_{NAME_UPPER}_RETRY_JITTER` | `retry_jitter` | `float` [0, 1) | `0.1` |

`lease_duration` covers a reader lease, a writer lease, and a writer intent.
