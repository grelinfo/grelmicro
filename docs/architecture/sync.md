# Synchronization Internals

This page documents the internal design of the [Synchronization Primitives](../sync.md).

## Worker Identity

Each synchronization primitive (`Lock`, `TaskLock`, `LeaderElection`) generates a unique **worker identity** at instantiation using `uuid1()` (UUIDv1).

UUIDv1 is based on the host MAC address, current timestamp, and a random 14-bit clock sequence. This combination ensures uniqueness across:

- **Multiple processes** (e.g., `uvicorn --workers N`): Each worker process imports the application independently, so `uuid1()` is called separately per process with distinct timestamps and clock sequences.
- **Multiple instances** within the same process: Each `Lock(...)` or `TaskLock(...)` call generates its own `uuid1()`, producing a different worker identity.

!!! info "Why UUIDv1 over UUIDv4?"
    `uuid1()` is ~2.5x faster than `uuid4()` because it derives values from the MAC address and timestamp rather than reading from the OS random number generator (`os.urandom`). Since the worker identity only requires uniqueness (not unpredictability), UUIDv1 is the better choice.

## Token Generation

Lock tokens identify **who** holds a lock. They are derived deterministically from the worker identity and the current execution context using `uuid3()` (UUIDv3, name-based with MD5):

| Primitive | Token | Scope |
|---|---|---|
| `Lock` | `uuid3(worker, task_id)` | Per async task |
| `Lock.from_thread` | `uuid3(worker, thread_id)` | Per thread |
| `TaskLock` | `uuid3(worker, task_id)` | Per async task |
| `TaskLock.from_thread` | `uuid3(worker, thread_id)` | Per thread |
| `LeaderElection` | `worker` directly | Per process |

This design provides the following guarantees:

- **Mutual exclusion**: Different async tasks or threads produce different tokens for the same lock, ensuring only one caller holds the lock at a time.
- **Re-entrancy**: The same async task (or thread) always produces the same deterministic token, allowing it to re-acquire the lock to extend the lease.
- **Isolation**: Different lock instances have different worker identities, so their tokens never collide even when used from the same task or thread.

## Lock Name and Backend Key

All synchronization primitives use the `name` parameter directly as the backend key. There is **no namespace or prefix** per lock type — a `Lock("my-resource")`, `TaskLock("my-resource")`, and `LeaderElection("my-resource")` would all compete for the same backend entry.

!!! warning
    Use distinct names across all synchronization primitive instances to avoid unintended conflicts. For example, prefix names by purpose: `"lock:my-resource"`, `"task:cleanup"`, `"leader:main"`.
