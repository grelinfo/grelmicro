# Synchronization Primitives

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends (see more in the Backends section).

The available primitives are:

- **[Leader Election](#leader-election)**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **[Lock](#lock)**: A distributed lock that can be used to synchronize access to shared resources.

The synchronization primitives can be used in combination with the `TaskManager` and `TaskRouter` to control task execution in a distributed system (see more in [Task Scheduler](task.md)).

## Backend

You must load a synchronization backend before using synchronization primitives.

!!! note
    Although Grelmicro use AnyIO for concurrency, the backends generally depend on `asyncio`, therefore Trio is not supported.

You can initialize a backend like this:

=== "Redis"
    ```python
    {!> ../examples/sync/redis.py!}
    ```

=== "Postgres"
    ```python
    {!> ../examples/sync/postgres.py!}
    ```

=== "Memory (For Testing Only)"
    ```python
    {!> ../examples/sync/memory.py!}
    ```

!!! warning
    Please make sure to use a proper way to store connection url, such as environment variables (not like the example above).

!!! tip
    Feel free to create your own backend and contribute it. In the `sync.abc` module, you can find the protocol for creating new backends.



## Leader Election

Leader election ensures that only one worker in the cluster is designated as the leader at any given time using a distributed lock.

The leader election service is responsible for acquiring and renewing the distributed lock. It runs as an AnyIO Task that can be easily started with the [Task Manager](./task.md#task-manager). This service operates in the background, automatically renewing the lock to prevent other workers from acquiring it. The lock is released automatically when the task is cancelled or during shutdown.

=== "Task Manager (Recommended)"
    ```python
    {!> ../examples/sync/leaderelection_task.py!}
    ```

=== "AnyIO Task Group (Advanced)"
    ```python
    {!> ../examples/sync/leaderelection_anyio.py!}
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
{!> ../examples/sync/lock.py!}
```

!!! warning
    The lock is designed for use within an async event loop and is not thread-safe or process-safe.

