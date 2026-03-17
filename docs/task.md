# Task Scheduler

The `task` package provides a simple task scheduler that can be used to run tasks periodically.

> **Note**: This is not a replacement for bigger tools like Celery, taskiq, or APScheduler. It is just lightweight, easy to use, and safe for running tasks in a distributed system with synchronization.

The key features are:

- **Fast & Easy**: Offers simple decorators to define and schedule tasks effortlessly.
- **Interval Task**: Allows tasks to run at specified intervals.
- **Synchronization**: Controls concurrency using synchronization primitives to manage simultaneous task execution (see the `sync` package).
- **Dependency Injection**: Use [FastDepends](https://lancetnik.github.io/FastDepends/) library to inject dependencies into tasks.
- **Error Handling**: Catches and logs errors, ensuring that task execution errors do not stop the scheduling.

## Task Manager

The `TaskManager` class is the main entry point to manage scheduled tasks. You need to start the task manager to run the scheduled tasks using the application lifespan.

=== "FastAPI"

    ```python
    --8<-- "task/fastapi.py"
    ```

=== "FastStream"

    ```python

    --8<-- "task/faststream.py"
    ```

## Interval Task

To create an `IntervalTask`, use the `interval` decorator method of the `TaskManager` instance. This decorator allows tasks to run at specified intervals.

> **Note**: The interval specifies the waiting time between task executions. Ensure that the task execution duration is considered to meet deadlines effectively.

=== "TaskManager"

    ```python
    --8<-- "task/interval_manager.py"
    ```

=== "TaskRouter"

    ```python
    --8<-- "task/interval_router.py"
    ```

### Distributed Lock

To create a distributed interval task that runs at most once per interval across all workers, set `max_lock_seconds` on the `interval` decorator. This enables a built-in [`TaskLock`](sync.md#task-lock), so there is no need to configure synchronization manually.

```python
--8<-- "task/interval_lock.py"
```

- **`seconds`**: The duration in seconds between each scheduling attempt. Each worker retries every N seconds, but only one worker executes per interval thanks to the built-in lock.
- **`max_lock_seconds`**: Crash protection TTL. Must be >= `seconds`. If a worker crashes while holding the lock, the lock expires after this duration and another worker can take over.
- **`min_lock_seconds`**: Minimum duration to hold the lock after task completion. Prevents re-execution on other nodes too soon. Defaults to `seconds`.

### With Leader Gating

You can optionally gate the task behind a [Leader Election](sync.md#leader-election). The task will only execute on the leader worker. Setting `leader` implies distributed locking (with `max_lock_seconds` defaulting to `seconds * 5`):

```python
--8<-- "task/interval_leader.py"
```

### Custom Lock Timing

For long-running tasks, you can customize both `max_lock_seconds` and `min_lock_seconds`:

```python
--8<-- "task/interval_lock_custom.py"
```

### How It Works

When the lock is already held, the task skips the execution (logged at DEBUG level) and retries on the next interval.

```
Node A:  [acquire] → [execute] → [hold for seconds] → [TTL expires]
Node B:  [skip] → ... → [skip] → ... → [acquire] → [execute]
```

### Synchronization Acquisition Order

When combining leader gating, distributed locking, and a resource lock, the synchronization primitives are acquired in the following order (outermost to innermost):

| Order | Primitive | Purpose |
|-------|-----------|---------|
| 1 | [`LeaderElection`](sync.md#leader-election) | Instantly rejects non-leader workers without touching any lock, avoiding unnecessary contention. |
| 2 | [`TaskLock`](sync.md#task-lock) | Guarantees at-most-once execution per interval via a TTL-based distributed lock. Acquired after leadership is confirmed to keep the TTL window tight. |
| 3 | [`Lock`](sync.md#lock) | User-provided lock for shared-resource access. Acquired last so the resource is held only during actual execution, minimizing contention. |

Each primitive is only acquired if the previous one succeeded. For example, a non-leader worker is rejected at step 1 and never touches the task lock or resource lock.

## Scheduled Task (Deprecated)

!!! warning "Deprecated"
    The `scheduled()` decorator is deprecated. Use `interval()` with `max_lock_seconds` or `leader` instead.

The `scheduled()` decorator still works but emits a `DeprecationWarning`. It is equivalent to `interval(seconds=N, max_lock_seconds=N*5, min_lock_seconds=N)`.

## Synchronization

The `sync` parameter on `interval()` accepts a [`Lock`](sync.md#lock) to synchronize access to a shared resource during task execution.

!!! warning "Deprecated for TaskLock and LeaderElection"
    Using `sync` with `TaskLock` or `LeaderElection` is deprecated. Use `max_lock_seconds` and `leader` parameters instead.

See [Synchronization Primitives](sync.md) for more details.

## Task Router

For bigger applications, you can use the `TaskRouter` class to manage tasks in different modules.


```python
--8<-- "task/router.py:1-10"
```

Then you can include the `TaskRouter` into the `TaskManager` or other routers using the `include_router` method.

```python
--8<-- "task/router.py:12"
```

!!! tip
    The `TaskRouter` follows the same philosophy as the `APIRouter` in FastAPI or the **Router** in FastStream.
