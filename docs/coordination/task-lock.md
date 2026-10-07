# Task Lock

The Task Lock is a distributed lock for scheduled tasks. Unlike a regular
[`Lock`](lock.md), it does not release immediately. It keeps the lock held for a
configurable minimum duration to stop re-execution on other nodes.

As the `gate` of an interval task, the task extends the lease from the moment it
holds the lock until the body ends, so `lease_duration` only bounds how long a
crashed worker keeps it. Entered directly with `async with`, the lock relies on
the TTL (`lease_duration`) set at acquire time. If the body runs longer than
`lease_duration`, the lock expires and another node may acquire it.

- **`min_hold_duration`**: minimum duration to hold the lock after the task
  completes. Stops another node from re-executing too soon.
- **`lease_duration`**: maximum duration to hold the lock. Acts as a TTL for
  crash and deadlock protection.

Call `extend()` on a `TaskLock` to extend the lease while the task body is
still running. It raises `LockNotOwnedError` when the lease was lost, and
`LockExtendError` when the backend call fails:

```python title="fragment"
async with task_lock:
    await long_operation_part1()
    await task_lock.extend()  # before lease_duration elapses
    await long_operation_part2()
```

!!! tip
    For interval tasks, pass the lock as the
    [`gate` of `every()`](../task.md#tune-the-claim). A lock still named
    `"default"` takes the task name, and the task extends it while the body
    runs, so you never call `extend()` yourself. Most tasks need no
    `TaskLock` at all: [`gate="claim"`](../task.md#claim) builds one sized
    to the interval. Cron tasks never take a lock. They claim each fire
    against the schedule backend instead.

!!! warning
    When the lock expires before the task completes (`lease_duration`
    exceeded), another node may acquire the lock and execute concurrently. A
    warning is logged in this case.
