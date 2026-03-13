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


## Scheduled Task

To create a distributed scheduled task that runs at most once per interval across all workers, use the `scheduled` decorator. It has a built-in [`TaskLock`](sync.md#task-lock), so there is no need to configure synchronization manually.

```python
--8<-- "task/scheduled.py"
```

- **`seconds`**: The duration in seconds between each scheduling attempt. Each worker retries every N seconds, but only one worker executes per interval thanks to the built-in lock. Also used as the `lock_at_least_for` value.
- **`lock_at_most_for`**: Crash protection TTL (defaults to `seconds * 5`). Must be >= `seconds`. If a worker crashes while holding the lock, the lock expires after this duration and another worker can take over.

### With Leader Gating

You can optionally gate the task behind a [Leader Election](sync.md#leader-election). The task will only execute on the leader worker:

```python
--8<-- "task/scheduled_leader.py"
```

### Custom Crash Protection

For long-running tasks, increase `lock_at_most_for` to avoid premature lock expiration:

```python
--8<-- "task/scheduled_custom.py"
```

### How It Works

When the lock is already held, `ScheduledTask` skips the execution (logged at DEBUG level) and retries on the next interval.

```
Node A:  [acquire] → [execute] → [hold for seconds] → [TTL expires]
Node B:  [skip] → ... → [skip] → ... → [acquire] → [execute]
```

## Synchronization (Deprecated)

!!! warning "Deprecated"
    The `sync` parameter on `interval()` is deprecated. Use the [`scheduled()`](#scheduled-task) decorator instead for distributed task execution with built-in TaskLock.

See [Synchronization Primitives](sync.md) for the standalone `TaskLock` API.

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
