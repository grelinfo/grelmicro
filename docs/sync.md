# Synchronization Primitives

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends (see more in the Backends section).

The available primitives are:

- **[Task Lock](#task-lock)**: A distributed lock for scheduled tasks with minimum and maximum hold times.
- **[Leader Election](#leader-election)**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **[Lock](#lock)**: A distributed lock that can be used to synchronize access to shared resources.

The synchronization primitives can be used in combination with the `TaskManager` and `TaskRouter` to control task execution in a distributed system (see more in [Task Scheduler](task.md)).

## Backend

You must load a synchronization backend before using synchronization primitives.

!!! note
    Although grelmicro uses AnyIO for concurrency, the backends generally depend on `asyncio`, therefore Trio is not supported.

You can initialize a backend like this:

=== "Redis"
    ```python
    --8<-- "sync/redis.py"
    ```

=== "Postgres"
    ```python
    --8<-- "sync/postgres.py"
    ```

=== "Memory (For Testing Only)"
    ```python
    --8<-- "sync/memory.py"
    ```

!!! warning
    Please make sure to use a proper way to store connection URLs, such as environment variables (not like the example above).

!!! tip
    Feel free to create your own backend and contribute it. In the `sync.abc` module, you can find the protocol for creating new backends.



## Task Lock

The Task Lock is a distributed lock designed for scheduled tasks. Unlike a regular Lock, it does not release immediately. Instead, it keeps the lock held for a configurable minimum duration to prevent re-execution on other nodes.

There is no background task that maintains the lock active during execution. The lock relies entirely on the TTL (`max_lock_seconds`) set at acquire time. If the task runs longer than `max_lock_seconds`, the lock expires and another node may acquire it.

- **`min_lock_seconds`**: Minimum duration to hold the lock after task completion. Prevents another node from re-executing too soon.
- **`max_lock_seconds`**: Maximum duration to hold the lock. Acts as a TTL for crash/deadlock protection.

!!! tip
    For scheduled tasks, prefer the [`interval()` decorator with `max_lock_seconds`](task.md#distributed-lock) which configures a `TaskLock` automatically with sensible defaults.

!!! warning
    When the lock expires before the task completes (`max_lock_seconds` exceeded), another node may acquire the lock and execute concurrently. A warning is logged in this case.


## Leader Election

Leader election ensures that only one worker in the cluster is designated as the leader at any given time using a distributed lock.

The leader election service is responsible for acquiring and renewing the distributed lock. It runs as an AnyIO Task that can be easily started with the [Task Manager](./task.md#task-manager). This service operates in the background, automatically renewing the lock to prevent other workers from acquiring it. The lock is released automatically when the task is cancelled or during shutdown.

=== "Task Manager (Recommended)"
    ```python
    --8<-- "sync/leaderelection_task.py"
    ```

=== "AnyIO Task Group (Advanced)"
    ```python
    --8<-- "sync/leaderelection_anyio.py"
    ```

## Lock

The lock is a distributed lock that can be used to synchronize access to shared resources.

The lock supports the following features:

- **Async**: The lock must be acquired and released asynchronously.
- **Distributed**: The lock must be distributed across multiple workers.
- **Reentrant**: The lock must allow the same token to acquire it multiple times to extend the lease.
- **Expiring**: The lock must have a timeout to auto-release after an interval to prevent deadlocks.
- **Non-blocking**: Lock operations must not block the async event loop.
- **Vendor-agnostic**: Must support multiple backends (Redis, Postgres, ConfigMap, etc.).


```python
--8<-- "sync/lock.py"
```

!!! warning
    The lock is designed for use within an async event loop and is not thread-safe or process-safe.

!!! tip "Want to understand how worker identity and lock tokens work internally?"
    See [Synchronization Internals](architecture/sync.md) for details on UUID generation, token scoping, and design guarantees.
