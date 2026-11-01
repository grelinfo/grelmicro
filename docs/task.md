# Task Scheduler

A simple scheduler that runs tasks periodically. Use it for lightweight recurring jobs without a full task queue.

- **Fast and easy**: simple decorators to define and schedule tasks with minimal boilerplate.
- **Interval tasks**: run tasks at fixed intervals.
- **Cron tasks**: run tasks on a cron schedule in the timezone you choose.
- **On-demand jobs**: run work when a request asks for it, and follow its progress until it ends.
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
| Run work on request and follow its progress | [`@tasks.job(...)`](#on-demand-jobs) |

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

--8<-- "env_gate.md"

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

A cron task that leaves `gate` out while a schedule backend is registered also warns each time it starts, with [`ungated-cron`](diagnostics.md#ungated-cron), except in `development` and `test`. Pass `gate=None` to say every worker on purpose.

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

## On-demand jobs

A job runs once each time your code submits it, and reports its progress while
it runs. Use it for work started from a request that takes too long to answer
in it: an import, a report, a batch handled item by item.

```python
--8<-- "task/job.py:1:48"
```

`@tasks.job` turns the function into a `Job`. Call `submit` with the function's
own arguments, after `progress`. It returns at once with a `JobInfo`, and the
job runs in the background, in the context of the code that submitted it, so its
logs and traces carry that request's fields. A `JobInfo` is a Pydantic model, so an endpoint
returns it as is:

```json
{
  "id": "6Rk2p0N8yq1vQm3ZcXbE4w",
  "name": "app:import_rows",
  "status": "running",
  "done": 2,
  "total": 3,
  "results": ["row-1", "row-2"],
  "error": null,
  "cancel_requested": false,
  "created_at": "2026-11-02T09:15:00Z",
  "started_at": "2026-11-02T09:15:00Z",
  "finished_at": null
}
```

!!! warning "One process"
    A job lives in the process that runs it. Behind a load balancer, a poll that
    reaches another replica finds nothing. Route a job's requests to one
    replica, or run one replica.

### Status

| `status` | Meaning |
| --- | --- |
| `queued` | Waiting for a free slot. |
| `running` | The body is running. |
| `succeeded` | The body returned. |
| `failed` | The body raised. `error` says why. |
| `cancelled` | Cancelled before or while it ran. |

A job only moves forward: `queued`, then `running`, then one of the last three.
`finished_at` is set when it reaches one of them.

### Progress

Take `progress: Progress` as the first parameter. Set `progress.total` when you
know it, and call `progress.advance()` after each item. Pass `result=` to add
the item's result to `results`. A result must be JSON: a string, a number, a
boolean, or a list or dict of them.

Results reported before a failure or a cancel stay in `results`. `results` holds
every result the job reports, so a job over millions of items should report a
summary instead.

The arguments after `progress` reach the body as you pass them, checked by your
type checker and never copied. `submit` raises `TypeError` for arguments the
body does not take. A dependency the body takes from
[FastDepends](https://lancetnik.github.io/FastDepends/) is injected, never
submitted. Give it a default, `client: Client = Depends(get_client)`, so your
type checker does not ask `submit` for it either.

### Errors

A job that raises ends `failed`. The caller sees `"Job failed"`, and the full
traceback goes to the log. To show the caller a message, raise `JobError`:

```python
raise JobError(f"Row {row.number} is invalid")
```

Its message reaches `error` as written. Write it for the caller. A `JobError`
logs one warning line, with no traceback. Inside a task group in the body, it
counts when every error the group raised is a `JobError`, and the first message
is shown.

### Owner

`import_rows.for_owner(user_id)` is the job as one caller sees it, with the same
`submit`, `get` and `cancel`. Use it to keep each caller to their own jobs.
`get` and `cancel` return `None` for a job that does not exist, has expired, or
belongs to another owner, so a caller cannot tell them apart. Answer `404` in
all three cases.

`import_rows` itself, without `for_owner`, finds only the jobs submitted
without an owner. An endpoint that forgets `for_owner` finds nothing, never
another caller's job.

### Cancel

`cancel` asks the job to stop and returns at once with `cancel_requested`
set. A queued job ends `cancelled` right away. A running async body gets
`asyncio.CancelledError` at its next `await` and ends `cancelled`, even if it
catches the error and returns. Cancelling a finished job changes nothing and
returns it as it is.

A sync body runs in a worker thread, which nothing can interrupt. It stops at
its next `progress.checkpoint()`, so call it between items. Until then the job
reads `running`.

### Stop between items

`progress.checkpoint()` raises `asyncio.CancelledError` once the job should
stop: a cancel was requested, or the service is shutting down. Call it where
stopping is safe, between two items. The job then ends `cancelled`, with the
results reported so far.

To save state before stopping, read `progress.stopping` instead:

```python title="fragment"
for row in rows:
    if progress.stopping:
        await save_cursor(upload_id, row.number)
        progress.checkpoint()
    progress.advance(result=await save_row(row))
```

A body that returns ends `succeeded`, so stop with `checkpoint()`, never with a
bare `return`.

### Limits

| Parameter | Default | Meaning |
| --- | --- | --- |
| `max_concurrent` | `1` | Jobs of this function running at once, in this process. |
| `max_queued` | `100` | Jobs waiting for a slot. `0` refuses as soon as every slot runs. `None` means no limit. |
| `keep` | `3600` | How long a finished job stays readable, in seconds or a `timedelta`. `0` keeps nothing. `None` means no limit. |

When the queue is full, or the service is shutting down, `submit` raises
`JobRefusedError`. With [error responses](http/errors.md) installed, it answers
`503`, so the client retries, on another replica if there is one.

A sync body holds a thread of the default executor for its whole run, as sync
scheduled tasks do. Keep `max_concurrent` low for long sync jobs.

### Shutdown

Leaving `async with tasks` drains jobs with the scheduled tasks, within the
same `shutdown_timeout`:

1. `submit` refuses new jobs with `JobRefusedError`.
2. Queued jobs end `cancelled` without starting.
3. Running jobs see `progress.stopping` turn `True`. Each stops at its next
   `checkpoint()`, after the item in hand, and releases its locks through
   `async with` as it unwinds.
4. A job still running after `shutdown_timeout` is cancelled at its next
   `await`. A sync job ends `cancelled` at once, and its thread stops at its
   next `checkpoint()`, `advance()` or `total` update.

A `Grelmicro` app closes `Tasks` before the rest of what it uses, so a draining
job still reaches its database. A sync body that never calls `checkpoint()`
cannot be stopped: its thread keeps the process alive until it returns or the
orchestrator kills it.

A job stopped by shutdown is not resumed. The client sees it `cancelled` while
the process lives, and `404` once it is gone. Submit it again, starting from a
cursor the body saved.

### Metrics

Each run of a job lands on
[`grelmicro.task.runs`](metrics.md#every-fire-lands-on-grelmicrotaskruns) under
the job's name, like a scheduled fire. A cancelled run counts no run, and its
`grelmicro.task.duration` reads as an error.

!!! note "The job decorator goes on top"
    `@tasks.job` returns a `Job`, not the function. A decorator written below it
    wraps every run. A grelmicro decorator written above it receives the `Job`
    and refuses with a `TypeError`.

## Task Introspection

Each interval and cron task exposes two read-only properties for
observability:

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

# After startup: tasks.tasks holds IntervalTask and CronTask objects,
# and a Job for each @tasks.job. `every` and `cron` return the original
# function unchanged.
task = tasks.tasks[-1]
info: FireInfo | None = task.last_fire
if info is not None:
    print(info.outcome, info.duration)

next_fire = task.next_fire_time  # None until the first loop iteration
```

## Check what is scheduled

`micro.describe().tasks` lists every task the app's `Tasks` holds, included routers too, so a test can pin both the wiring and the gate:

```python
--8<-- "task/check_scheduled.py"
```

Each entry is a `TaskReport` with the schedule, the gate (`"claim"`, `"leader"` or `"lock"`), the leader election's name, the name it claims under (`claim_name`), and the backend it claims through. The tuple is empty when no task is registered. `python -m grelmicro check` prints the same list, see [See what got wired](wiring.md#see-what-got-wired).

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
