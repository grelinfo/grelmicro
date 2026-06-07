# Task Scheduler

A simple scheduler that runs tasks periodically. Use it for lightweight recurring jobs without a full task queue.

- **Fast and easy**: simple decorators to define and schedule tasks with minimal boilerplate.
- **Interval tasks**: run tasks at fixed intervals, locally or across a cluster.
- **Coordination**: control concurrency with distributed primitives (see [Coordination primitives](coordination.md)).
- **Dependency injection**: use [FastDepends](https://lancetnik.github.io/FastDepends/) to inject dependencies into tasks.
- **Error handling**: errors are caught and logged, so a failing task does not stop the scheduler.

## Quick start

Register a `Tasks` instance with a `Grelmicro` app, then schedule a task with the `interval` decorator:

```python
from grelmicro import Grelmicro
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(uses=[tasks])

@tasks.interval(seconds=5)
async def cleanup() -> None:
    ...

async with micro:
    ...
```

!!! warning "Per-process by default"
    `Tasks` runs schedules **in the local process only**. Every process that boots a `Tasks` instance runs its own copy of every registered task. To run a task at most once across the fleet, gate it with [`TaskLock`](coordination.md#task-lock) or [`LeaderElection`](coordination.md#leader-election). Without one of those, a 3-replica deployment runs the same `@tasks.interval(...)` three times per tick.

!!! note
    This is not a replacement for full task queues such as Celery, taskiq, or APScheduler. It is small, simple, and safe for running tasks in a distributed system.

## Tasks

The `Tasks` class is the main entry point to manage tasks. The recommended way to lifecycle it is to register it with a `Grelmicro` app, as shown in the quick start above.

`Grelmicro.use(item)` (or the `uses=` constructor kwarg) accepts any async context manager and lifecycles it with the app. The caller keeps the reference and uses the manager directly.

Start it standalone using the application lifespan:

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

!!! tip "Sensitive workflows: pass an explicit `name=`"
    When `name=` is omitted, the task reference is derived from the function's
    `module:qualname`. That reference appears in logs, distributed
    coordination keys (when `TaskLock` is used), and metric labels.
    Pass an explicit `name="..."` for tasks that handle credentials,
    customer data, or other workflows where the internal module path
    should not leak through operational surfaces.

=== "Tasks"

    ```python
    --8<-- "task/interval_manager.py"
    ```

=== "TaskRouter"

    ```python
    --8<-- "task/interval_router.py"
    ```

### Distributed Lock

Set `max_lock_seconds` to enable distributed locking: the task runs at most once per interval across all workers. This uses a built-in [`TaskLock`](coordination.md#task-lock) automatically.

```python
--8<-- "task/interval_lock.py"
```

| Parameter | Description |
|-----------|-------------|
| `seconds` | Duration in seconds between each scheduling attempt. Each worker retries every N seconds, but only one executes per interval. |
| `max_lock_seconds` | Crash protection TTL. Must be >= `seconds`. If a worker crashes, the lock expires after this duration. |
| `min_lock_seconds` | Minimum duration to hold the lock after task completion. Prevents re-execution on other nodes too soon. Defaults to `seconds`. |

### Leader Gating

Restrict the task to the leader worker with a [Leader Election](coordination.md#leader-election), so only one worker executes it. Setting `leader` also enables distributed locking, with `max_lock_seconds` defaulting to `seconds * 5`:

```python
--8<-- "task/interval_leader.py"
```

### Custom Lock Timing

For long-running tasks, customize both `max_lock_seconds` and `min_lock_seconds`:

```python
--8<-- "task/interval_lock_custom.py"
```

### Resource Lock

Combine distributed locking with a [`Lock`](coordination.md#lock) to synchronize access to a shared resource during task execution. Pass the `Lock` via the `sync` parameter:

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
| 1 | [`LeaderElection`](coordination.md#leader-election) | Rejects non-leader workers immediately without acquiring any lock, which avoids unnecessary contention. |
| 2 | [`TaskLock`](coordination.md#task-lock) | Guarantees at-most-once execution per interval. It is acquired after leadership is confirmed so the TTL window stays short. |
| 3 | [`Lock`](coordination.md#lock) | User-provided lock for shared-resource access. It is acquired last so the resource is held only during actual execution. |

Each primitive is only acquired if the previous one succeeded. For example, a non-leader worker is rejected at step 1 and never touches the task lock or resource lock.

## Cron Task

Use the `cron` decorator to run a task on a cron schedule:

```python
--8<-- "task/cron.py"
```

The expression has five fields: `minute hour day-of-month month day-of-week`. The example above runs every day at 02:00 in the `Europe/Zurich` timezone. The `timezone` defaults to `"UTC"`.

Each field accepts:

| Syntax | Meaning |
|--------|---------|
| `*` | Every value |
| `*/15` | Every 15th value (a step) |
| `9-17` | A range |
| `9-17/2` | Every second value in a range |
| `1,15,45` | A list of values |
| `5` | A single value |

Field ranges are minute `0-59`, hour `0-23`, day-of-month `1-31`, month `1-12`, and day-of-week `0-6` where `0` is Sunday. The value `7` also means Sunday.

!!! note "Day-of-month and day-of-week"
    When both `day-of-month` and `day-of-week` are restricted (neither is `*`), a day matches if it matches **either** field. For example, `0 0 15 * 1` runs on the 15th of the month and on every Monday. When only one is restricted, only that one applies.

### Distributed cron

With a [`Coordination`](coordination.md) component wired, every fire is claimed against durable state, so the task runs at most once across all workers per fire:

```python
@task.cron("*/5 * * * *")
async def sync_data():
    ...
```

The schedule backend stores the last fire on the provider (Redis, Postgres, and SQLite all ship today). Because that state is durable, a fire missed while every worker was down replays once when a worker comes back. Only the most recent missed fire runs, never a backlog of skipped ones. Without a backend, the task runs on every worker, every fire. Kubernetes is intentionally not provided: use a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

Set `misfire_grace_seconds` to bound how late a missed fire may run:

```python
@task.cron("0 * * * *", misfire_grace_seconds=600)
async def hourly_rollup():
    ...
```

A fire more than 600 seconds late is dropped instead of replayed. The default is `None`, which replays any missed fire however late.

!!! warning "Make the body idempotent"
    The guarantee is at-most-once. A worker that claims a fire and then crashes mid-run does not retry it, because the last-fire state already advanced. Make the body idempotent, or wrap it with [`@retry`](resilience.md), when correctness depends on completion.

### Cron in distributed systems

On Kubernetes, when the task is a batch job and you can define manifests, prefer a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) that runs a one-shot command. It is the platform's job and the least code. Grelmicro does not create CronJob resources and should not, since that needs cluster-write permissions an application should not hold.

Use grelmicro `@cron` when you want the task to run inside the live service with its warm connections and dependencies, or want one scheduling model across Redis, Postgres, SQLite, and bare metal.

## Task Router

For bigger applications, use the `TaskRouter` class to organize tasks across modules:

```python
--8<-- "task/router.py:1-10"
```

Then include the `TaskRouter` into the `Tasks` or other routers:

```python
--8<-- "task/router.py:12"
```

!!! tip
    The `TaskRouter` follows the same philosophy as the `APIRouter` in FastAPI or the **Router** in FastStream.

See [Coordination primitives](coordination.md) for more details.
