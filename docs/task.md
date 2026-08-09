# Task Scheduler

A simple scheduler that runs tasks periodically. Use it for lightweight recurring jobs without a full task queue.

- **Fast and easy**: simple decorators to define and schedule tasks with minimal boilerplate.
- **Interval tasks**: run tasks at fixed intervals, locally or across a cluster.
- **Coordination**: control concurrency with distributed primitives (see [Coordination primitives](coordination.md)).
- **Dependency injection**: use [FastDepends](https://lancetnik.github.io/FastDepends/) to inject dependencies into tasks.
- **Error handling**: errors are caught and logged, so a failing task does not stop the scheduler.

## Quick start

Register a `Tasks` instance with a `Grelmicro` app, then schedule a task with the `every` decorator:

```python
from grelmicro import Grelmicro
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(uses=[tasks])

@tasks.every(seconds=5)
async def cleanup() -> None:
    ...

async with micro:
    ...
```

!!! warning "Per-process by default"
    `Tasks` runs schedules **in the local process only**. Every process that boots a `Tasks` instance runs its own copy of every registered task. To run an interval task at most once across the fleet, gate it with [`TaskLock`](coordination.md#task-lock) or [`LeaderElection`](coordination.md#leader-election). Without one of those, a 3-replica deployment runs the same `@tasks.every(...)` three times per tick. Cron tasks work differently. They claim each fire against the schedule backend, so a wired [`Coordination`](coordination.md) component is all they need.

!!! note
    This is not a replacement for full task queues such as Celery, taskiq, or APScheduler. It is small, simple, and safe for running tasks in a distributed system.

## Tasks

The `Tasks` class is the main entry point to manage tasks. The recommended way to lifecycle it is to register it with a `Grelmicro` app, as shown in the quick start above.

`Grelmicro.use(item)` (or the `uses=` constructor kwarg) accepts any async context manager and lifecycles it with the app. The caller keeps the reference and uses the manager directly.

Choose the entry point by the job:

| Need | Use |
|---|---|
| Simple recurring function | `@tasks.every(...)` |
| Group tasks across modules | `TaskRouter` |
| Run every cron task on one wall clock | `Tasks(timezone=...)` |
| Add an object that already implements the task protocol | `tasks.add_task(...)` |
| Run an interval task at most once across replicas | `@tasks.every(..., lock=TaskLock(...))` |
| Run a cron task at most once across replicas | `@tasks.cron(...)` with a [`Coordination`](coordination.md) component |
| Run only on the leader | `@tasks.every(..., leader=leader_election)` |

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

Use the `every` decorator to run a task at a fixed interval:

!!! note
    The interval specifies the waiting time between task executions. Ensure that the task execution duration is considered to meet deadlines effectively.

    The interval is measured from the end of one run to the start of the next (end-to-start). A run that takes longer than the interval pushes the next attempt back.

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

Pass a [`TaskLock`](coordination.md#task-lock) via `lock` to enable distributed locking: the task runs at most once per interval across all workers. The lock keeps its default `"default"` name, so the task name is used and you never repeat it.

```python
--8<-- "task/interval_lock.py"
```

| Parameter | Description |
|-----------|-------------|
| `seconds` | Duration between each scheduling attempt, as a number of seconds or a `timedelta`. Each worker retries every interval, but only one executes per interval. |
| `lock` | A `TaskLock` for at-most-once scheduling. Its `lease_duration` is the crash-protection TTL and must be >= `seconds`. Its `min_hold_duration` keeps the lock held after completion to prevent re-execution too soon. |

The `lock` is authoritative: its `lease_duration`, `min_hold_duration`, `backend`, and `worker` are used as set.

### Leader Gating

Restrict the task to the leader worker with a [Leader Election](coordination.md#leader-election), so only one worker executes it. Setting `leader` also enables distributed locking. Without a `lock`, one is configured with `lease_duration` of `seconds * 5` and `min_hold_duration` of `seconds`:

```python
--8<-- "task/interval_leader.py"
```

### Custom Lock Timing

For long-running tasks, customize both `lease_duration` and `min_hold_duration` on the `TaskLock`:

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

!!! note "Cron has no `lock` parameter"
    `every` needs a [`TaskLock`](coordination.md#task-lock) to run at most once across replicas. Cron does not. It claims each fire against the schedule backend instead, so at-most-once is automatic once a [`Coordination`](coordination.md) component is wired. See [Distributed cron](#distributed-cron).

    Pass `sync=` to hold a [`Lock`](coordination.md#lock) around a shared resource during the run.

The expression has five fields: `minute hour day-of-month month day-of-week`. The example above runs every day at 02:00.

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

### Timezone

Cron fires on wall-clock time, so it needs to know which clock. Set it once on
the `Tasks`, and every cron task uses it:

```python
--8<-- "task/cron_timezone.py"
```

The default is `UTC`. A deployment sets it without touching code through
`GREL_TIMEZONE`, the one variable that says what wall clock the whole service
runs on. See [Configuration](config.md#one-timezone-for-the-whole-service).

A `TaskRouter` takes the timezone of the `Tasks` that includes it, whatever
order the wiring happens in. Pass `TaskRouter(timezone=...)` to give one group
of tasks a different clock. Nearest declaration wins: the task, then its
router, then the `Tasks`.

Names are IANA names such as `Europe/Zurich`, in any casing. A name that no
timezone matches is rejected where you write it, not at the first fire.
grelmicro ignores the POSIX `TZ` variable on purpose, though `TZ` still decides
what a naive `datetime.now()` returns inside your task body. Prefer
`datetime.now(UTC)` there.

!!! note "Daylight saving"
    Every wall-clock match fires once. When the clocks go forward and 02:30
    never happens, a `30 2 * * *` task fires once just after the jump. When
    they go back and 02:30 happens twice, it fires on the first pass only.

    A sub-hourly schedule such as `*/15 * * * *` is quiet for the repeated
    hour rather than running through it twice. Use UTC for a task that must
    keep a steady interval across a transition.

### Distributed cron

With a [`Coordination`](coordination.md) component wired, every fire is claimed against durable state, so the task runs at most once across all workers per fire:

```python
@task.cron("*/5 * * * *")
async def sync_data():
    ...
```

The schedule backend stores the last fire on the provider (Redis, Postgres, and SQLite all ship today). Because that state is durable, a fire missed while every worker was down replays once when a worker comes back. Only the most recent missed fire runs, never a backlog of skipped ones. Without a backend, the task runs on every worker, every fire. Kubernetes is intentionally not provided: use a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

!!! warning "Do not gate a cron body on leadership"
    Winning the claim advances the durable last-fire state **before** the body runs. A body that returns early still consumes the fire, so the work is skipped and the next attempt is a whole cron period away:

    ```python
    @task.cron("0 3 * * *")
    async def nightly():
        if not leader.is_leader():
            return  # the fire is already claimed, so it is now lost
        await do_work()
    ```

    Nothing needs gating here. The claim already picks exactly one worker per fire.

Set `misfire_grace_seconds` to bound how late a missed fire may run:

```python
@task.cron("0 * * * *", misfire_grace_seconds=600)
async def hourly_rollup():
    ...
```

A fire more than 600 seconds late is dropped instead of replayed. The default is `None`, which replays any missed fire however late.

!!! warning "Make the body idempotent"
    The guarantee is at-most-once. A worker that claims a fire and then crashes mid-run does not retry it, because the last-fire state already advanced. Make the body idempotent, or wrap it with [`@retry`](resilience/retry.md), when correctness depends on completion.

### Cron in distributed systems

On Kubernetes, when the task is a batch job and you can define manifests, prefer a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) that runs a one-shot command. It is the platform's job and the least code. Grelmicro does not create CronJob resources and should not, since that needs cluster-write permissions an application should not hold.

Use grelmicro `@cron` when you want the task to run inside the live service with its warm connections and dependencies, or want one scheduling model across Redis, Postgres, SQLite, and bare metal.

## Task Introspection

Each task exposes two read-only properties for observability:

- **`timezone`**: the IANA timezone name a cron task fires on. `None` until
  the tasks start, unless the task declared one itself.
- **`next_fire_time`**: the next scheduled fire as a timezone-aware `datetime`,
  or `None` when the task has not started yet. For interval tasks, this is
  computed from the last loop instant. For cron tasks, it comes from the
  parsed expression.
- **`last_fire`**: a `FireInfo` with the `started_at` timestamp, outcome (a
  `FireOutcome` enum: `SUCCESS`, `ERROR`, `SKIPPED`, `MISSED`, or
  `COORDINATION_ERROR`), and duration in seconds. `None` before the first
  fire. `FireOutcome` is a `StrEnum`, so each member compares equal to its
  string value (`outcome == "success"`).

  A fire that never reached the body sets `last_fire` too, with a duration
  of `0.0`. So a task whose schedule backend went down reads as
  `COORDINATION_ERROR` rather than keeping yesterday's success on display.
  The same outcomes are counted on
  [`grelmicro.task.runs`](metrics.md#every-fire-lands-on-grelmicrotaskruns).

Access the task object via `tasks.tasks`:

```python
from grelmicro.task import FireInfo, Tasks

tasks = Tasks()

@tasks.every(seconds=60)
async def cleanup() -> None:
    ...

# After startup: tasks.tasks holds IntervalTask and CronTask objects.
# The decorator returns the original function unchanged.
task = tasks.tasks[-1]
info: FireInfo | None = task.last_fire
if info is not None:
    print(info.outcome, info.duration)

next_fire = task.next_fire_time  # None until the first loop iteration
```

## Task Router

For bigger applications, use the `TaskRouter` class to organize tasks across modules:

```python
--8<-- "task/router.py:1:10"
```

Then include the `TaskRouter` into the `Tasks` or other routers:

```python
--8<-- "task/router.py:12"
```

!!! tip
    The `TaskRouter` follows the same philosophy as the `APIRouter` in FastAPI or the **Router** in FastStream.

See [Coordination primitives](coordination.md) for more details.
