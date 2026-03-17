# Task Scheduler

The `task` package provides a simple task scheduler that can be used to run tasks periodically.

!!! note
    This is not a replacement for bigger tools like Celery, taskiq, or APScheduler. It is just lightweight, easy to use, and safe for running tasks in a distributed system with synchronization.

The key features are:

- **Fast & Easy**: Simple decorators to define and schedule tasks effortlessly.
- **Interval Task**: Run tasks at specified intervals, locally or distributed.
- **Synchronization**: Control concurrency using distributed primitives (see [Synchronization Primitives](sync.md)).
- **Dependency Injection**: Use [FastDepends](https://lancetnik.github.io/FastDepends/) to inject dependencies into tasks.
- **Error Handling**: Catches and logs errors so that task failures do not stop the scheduler.

## Task Manager

The `TaskManager` class is the main entry point to manage tasks. Start it using the application lifespan:

=== "FastAPI"

    ```python
    --8<-- "task/fastapi.py"
    ```

=== "FastStream"

    ```python

    --8<-- "task/faststream.py"
    ```

## Interval Task

Use the `interval` decorator to run a task at a fixed interval:

!!! note
    The interval specifies the waiting time between task executions. Ensure that the task execution duration is considered to meet deadlines effectively.

=== "TaskManager"

    ```python
    --8<-- "task/interval_manager.py"
    ```

=== "TaskRouter"

    ```python
    --8<-- "task/interval_router.py"
    ```

### Distributed Lock

Set `max_lock_seconds` to enable distributed locking: the task runs at most once per interval across all workers. This uses a built-in [`TaskLock`](sync.md#task-lock) automatically.

```python
--8<-- "task/interval_lock.py"
```

| Parameter | Description |
|-----------|-------------|
| `seconds` | Duration in seconds between each scheduling attempt. Each worker retries every N seconds, but only one executes per interval. |
| `max_lock_seconds` | Crash protection TTL. Must be >= `seconds`. If a worker crashes, the lock expires after this duration. |
| `min_lock_seconds` | Minimum duration to hold the lock after task completion. Prevents re-execution on other nodes too soon. Defaults to `seconds`. |

### Leader Gating

Gate the task behind a [Leader Election](sync.md#leader-election) so only the leader worker executes it. Setting `leader` implies distributed locking (with `max_lock_seconds` defaulting to `seconds * 5`):

```python
--8<-- "task/interval_leader.py"
```

### Custom Lock Timing

For long-running tasks, customize both `max_lock_seconds` and `min_lock_seconds`:

```python
--8<-- "task/interval_lock_custom.py"
```

### Resource Lock

Combine distributed locking with a [`Lock`](sync.md#lock) to synchronize access to a shared resource during task execution. Pass the `Lock` via the `sync` parameter:

```python
--8<-- "task/interval_lock_resource.py"
```

### How It Works

When the lock is already held, the task skips the execution (logged at DEBUG level) and retries on the next interval.

```
Node A:  [acquire] → [execute] → [hold for seconds] → [TTL expires]
Node B:  [skip] → ... → [skip] → ... → [acquire] → [execute]
```

When combining leader gating, distributed locking, and a resource lock, the synchronization primitives are acquired in this order:

| Order | Primitive | Purpose |
|-------|-----------|---------|
| 1 | [`LeaderElection`](sync.md#leader-election) | Instantly rejects non-leader workers without touching any lock, avoiding unnecessary contention. |
| 2 | [`TaskLock`](sync.md#task-lock) | Guarantees at-most-once execution per interval. Acquired after leadership is confirmed to keep the TTL window tight. |
| 3 | [`Lock`](sync.md#lock) | User-provided lock for shared-resource access. Acquired last so the resource is held only during actual execution. |

Each primitive is only acquired if the previous one succeeded. For example, a non-leader worker is rejected at step 1 and never touches the task lock or resource lock.

## Task Router

For bigger applications, use the `TaskRouter` class to organize tasks across modules:

```python
--8<-- "task/router.py:1-10"
```

Then include the `TaskRouter` into the `TaskManager` or other routers:

```python
--8<-- "task/router.py:12"
```

!!! tip
    The `TaskRouter` follows the same philosophy as the `APIRouter` in FastAPI or the **Router** in FastStream.

## Deprecated APIs

### Scheduled Task

!!! danger "Deprecated"
    The `scheduled()` decorator is deprecated. Use `interval()` with `max_lock_seconds` or `leader` instead.

The `scheduled()` decorator still works but emits a `DeprecationWarning`. It is equivalent to `interval(seconds=N, max_lock_seconds=N*5, min_lock_seconds=N)`.

### sync with TaskLock or LeaderElection

!!! danger "Deprecated"
    Using `sync` with `TaskLock` or `LeaderElection` is deprecated. Use `max_lock_seconds` and `leader` parameters instead.

See [Synchronization Primitives](sync.md) for more details.
