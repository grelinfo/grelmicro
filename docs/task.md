# Task Scheduler

A simple scheduler that runs tasks periodically. Use it for lightweight recurring jobs without a full task queue.

- **Fast and easy**: simple decorators to define and schedule tasks with minimal boilerplate.
- **Interval tasks**: run tasks at fixed intervals.
- **Cron tasks**: run tasks on a cron schedule in the timezone you choose.
- **One worker or every worker**: every worker runs a task by default. Pass `gate="claim"` or a leader election to run it on one.
- **Coordination**: control concurrency with distributed primitives (see [Coordination primitives](coordination/index.md)).
- **Dependency injection**: use [FastDepends](https://lancetnik.github.io/FastDepends/) to inject dependencies into tasks.
- **Error handling**: errors are caught and logged, so a failing task does not stop the scheduler.

## Quick start

Register a `Tasks` instance with a `Grelmicro` app, then schedule a task with the `every` decorator:

```python title="quickstart.py"
--8<-- "task/quickstart.py"
```

!!! warning "Per-process by default"
    `Tasks` runs schedules **in the local process only**. Every process that boots a `Tasks` instance runs its own copy of every registered task, so a 3-replica deployment runs the same task three times. To run it on one worker, pass a [`gate`](#run-on-one-worker): `gate="claim"` or a [`LeaderElection`](coordination/leader-election.md).

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
| Run a task on one worker per interval or fire | `@tasks.every(..., gate="claim")` or `@tasks.cron(..., gate="claim")` |
| Run a task on the leader only | `@tasks.every(..., gate=leader_election)` |

!!! warning "The task decorator goes on top"

    `@tasks.every` and `@tasks.cron` register the function they are handed
    and return that same one. A decorator written below one of them wraps
    the module-level name, while the schedule keeps holding the original,
    so it applies to direct calls and never to a scheduled run. Put the
    task decorator on top:

    ```python
    @tasks.every(interval=60)
    @retry(when=Exception, attempts=3)
    async def refresh_catalog() -> None: ...
    ```

    Written the other way round, `@retry` refuses with a `TypeError` that
    names the right order. The same holds for every grelmicro decorator,
    and for [`@health.check`](health.md) and
    [`@outbox.handler`](outbox/consumer.md), which register the same way.

Start it standalone using the application lifespan:

=== "FastAPI"

    ```python
    --8<-- "task/fastapi_app.py"
    ```

=== "FastStream"

    ```python

    --8<-- "task/faststream_app.py"
    ```

## Interval Task

Use the `every` decorator to run a task at a fixed interval. `interval` takes whole seconds or a `timedelta`, so `interval=60` runs it every minute and `interval=timedelta(milliseconds=500)` twice a second. A float is refused.

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

## Cron Task

Use the `cron` decorator to run a task on a cron schedule:

```python
--8<-- "task/cron.py"
```

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

### Missed fires

A claimed cron task (`gate="claim"` or a `LeaderElection`) stores its last fire on the schedule backend (Redis, Postgres, and SQLite all ship today). Because that state is durable, a fire missed while every worker was down replays once when a worker comes back. Only the most recent missed fire runs, never a backlog of skipped ones. Kubernetes is intentionally not provided: use a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

Set `misfire_grace` to bound how late a missed fire may run. It takes whole seconds or a `timedelta`:

```python
@tasks.cron("0 * * * *", gate="claim", misfire_grace=600)
async def hourly_rollup():
    ...
```

A fire more than 600 seconds late is dropped instead of replayed. The default is `None`, which replays any missed fire however late.

!!! warning "Make the body idempotent"
    The guarantee is at-most-once. A worker that claims a fire and then crashes mid-run does not retry it, because the last-fire state already advanced. Make the body idempotent, or wrap it with [`@retry`](resilience/retry.md), when correctness depends on completion.

### Cron in distributed systems

On Kubernetes, when the task is a batch job and you can define manifests, prefer a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) that runs a one-shot command. It is the platform's job and the least code. Grelmicro does not create CronJob resources and should not, since that needs cluster-write permissions an application should not hold.

Use grelmicro `@cron` when you want the task to run inside the live service with its warm connections and dependencies, or want one scheduling model across Redis, Postgres, SQLite, and bare metal.

## Run on one worker

Every worker runs a task by default. Pass `gate` to `every` or `cron` to pick which workers run it:

| `gate` | Who runs it |
|--------|-------------|
| `None` (default) | Every worker, every interval or fire. |
| `"claim"` | One worker per interval or fire, whichever claims it first. |
| A `LeaderElection` | The elected worker. It claims each interval or fire too, so a leader handover never runs one twice. |
| A `TaskLock` | One worker per interval, with the lock's own timing. `every` only. |

A gated task needs a [`Coordination`](coordination/index.md) component to hold its claims. Without one, every fire reports a `coordination_error` and runs nothing, so a missing backend never turns into a task that silently runs everywhere.

Each task logs its resolved gate once when it starts, for example `Task started (interval: 60s, gate: claim): cleanup`.

### Claim

```python
--8<-- "task/interval_claim.py"
```

```python
--8<-- "task/cron_claim.py"
```

An interval claim is a [`TaskLock`](coordination/task-lock.md) named after the task. The worker that wins it holds it for the whole interval, so a replica whose timer fires a moment later finds it taken and skips. A cron claim advances the durable last-fire state instead, which also [replays a missed fire](#missed-fires).

From the moment it holds the claim until the body ends, the task extends it every third of the lease, so a body, and a wait for a `sync` lock before it, may take as long as it needs. The lease (two intervals) only bounds how long a worker that crashed keeps the claim. If an extension cannot reach the backend, it is retried for as long as the lease lasts. A claim lost that way logs a warning and the body finishes, since stopping it mid-write would be worse than a second run. Work that must never overlap takes a [`Lock`](coordination/lock.md) as `sync` too.

### Leader

```python
--8<-- "task/interval_leader.py"
```

Add the [`LeaderElection`](coordination/leader-election.md) to the `Tasks` too, so it campaigns. A worker that is not the leader skips each interval or fire without touching the backend. A cron fire missed while no worker led, at startup or during a handover, replays once when a worker becomes the leader.

!!! warning "Gate on leadership with `gate`, not in the body"
    A cron claim advances the last-fire state **before** the body runs. A body that checks leadership and returns early still consumes the fire, so the work is lost until the next one:

    ```python
    @tasks.cron("0 3 * * *", gate="claim")
    async def nightly():
        if not leader.is_leader():
            return  # the fire is already claimed, so it is now lost
        await do_work()
    ```

    `gate=leader` checks leadership before the claim, so a follower never consumes a fire.

### Choosing a gate

- Pick `"claim"` for short, idempotent work any replica can do: a cleanup, a sync, a report.
- Pick a leader when the task keeps something between runs: a warm cache, an open subscription, or an order you must keep.

### Tune the claim

For an interval task, pass a [`TaskLock`](coordination/task-lock.md) to set the timing yourself:

```python
--8<-- "task/interval_lock_custom.py"
```

`min_hold_duration` must be at least `interval`, or a peer could claim the same interval once the body ends. A later `reconfigure` to a shorter one is refused too. `lease_duration` is how long a crashed worker keeps the claim, since the task extends it while the body runs. A lock without a name takes the task name, so you never repeat it. A task named after its function in a script run directly (`__main__:job`) locks under `task-__main__:job`, and an explicit task `name=` cannot start with `task-`. An external config reload tunes the lock under `GREL_TASKLOCK_{TASK}_`. The task uses the lock you pass, so the handle you keep is the one it holds.

Cron takes no `TaskLock`. Its claim is a compare-and-set on durable state, with nothing held while the body runs.

### Resource lock

`sync` holds a [`Lock`](coordination/lock.md) around the body once the gate lets the worker through. Use it to serialise access to a shared resource. It never decides which workers run the task, that is the gate's job.

```python
--8<-- "task/interval_lock_resource.py"
```

A worker is checked in this order, and stops at the first refusal:

| Order | Check | Purpose |
|-------|-------|---------|
| 1 | [`LeaderElection`](coordination/leader-election.md) | Rejects a worker that is not the leader without touching any backend. |
| 2 | Claim | One worker per interval or fire. |
| 3 | `sync` [`Lock`](coordination/lock.md) | Held only while the body runs. |

A refused interval is skipped (logged at DEBUG) and tried again on the next one.

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

@tasks.every(interval=60)
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

See [Coordination primitives](coordination/index.md) for more details.
