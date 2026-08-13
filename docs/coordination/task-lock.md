# Task Lock

The Task Lock is a distributed lock for scheduled tasks. Unlike a regular
[`Lock`](lock.md), it does not release immediately. It keeps the lock held for a
configurable minimum duration to stop re-execution on other nodes.

No background task keeps the lock active during execution. The lock relies on the
TTL (`lease_duration`) set at acquire time. If the task runs longer than
`lease_duration`, the lock expires and another node may acquire it.

- **`min_hold_duration`**: minimum duration to hold the lock after the task
  completes. Stops another node from re-executing too soon.
- **`lease_duration`**: maximum duration to hold the lock. Acts as a TTL for
  crash and deadlock protection.

Call `refresh()` on a `TaskLock` to renew the lease while the task body is
still running. Raises `LockNotOwnedError` when the lease was lost:

```python
async with task_lock:
    await long_operation_part1()
    await task_lock.refresh()  # extend before lease_duration elapses
    await long_operation_part2()
```

!!! tip
    For interval tasks, prefer the
    [`every()` decorator with `lock=TaskLock(...)`](../task.md#distributed-lock),
    which re-stamps the lock with the task name automatically. Cron tasks need
    no lock at all. They claim each fire against the
    [schedule backend](../task.md#distributed-cron) instead.

!!! warning
    When the lock expires before the task completes (`lease_duration`
    exceeded), another node may acquire the lock and execute concurrently. A
    warning is logged in this case.
