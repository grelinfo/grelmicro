# Synchronization Primitives

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends (see more in the Backends section).

The available primitives are:

- **[Task Lock](#task-lock)**: A distributed lock for scheduled tasks with minimum and maximum hold times.
- **[Leader Election](#leader-election)**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **[Lock](#lock)**: A distributed lock that can be used to synchronize access to shared resources.

The synchronization primitives can be used in combination with the `TaskManager` and `TaskRouter` to control task execution in a distributed system (see more in [Task Scheduler](task.md)).

!!! warning "Thread Safety"
    All synchronization primitives (`Lock`, `TaskLock`, `LeaderElection`) are designed for use within a single async event loop and are **not thread-safe**. Sync access from worker threads is supported via `from_thread` adapters, which dispatch operations to the event loop. Do not share instances across multiple event loops or threads without the adapter.

## Backend

You must load a synchronization backend before using synchronization primitives.

!!! tip "Install"
    Each backend needs its own extra:

    - Redis: `pip install "grelmicro[redis]"`
    - PostgreSQL: `pip install "grelmicro[postgres]"`
    - SQLite: `pip install "grelmicro[sqlite]"`
    - Kubernetes: `pip install "grelmicro[kubernetes]"`

    See the [installation guide](installation.md) for `uv` and `poetry`.

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

=== "Kubernetes"
    ```python
    --8<-- "sync/kubernetes.py"
    ```

=== "SQLite"
    ```python
    --8<-- "sync/sqlite.py"
    ```

=== "Memory"
    ```python
    --8<-- "sync/memory.py"
    ```

!!! warning
    Please make sure to use a proper way to store connection URLs, such as environment variables (not like the example above).

| | Redis | PostgreSQL | Kubernetes | SQLite | Memory |
|---|---|---|---|---|---|
| **Use case** | Production | Production | Production (K8s-native) | Home lab / Local testing | Testing only |
| **Multi-node** | Yes | Yes | Yes | No | No |
| **Persistence** | Yes | Yes | Yes (etcd-backed) | Yes | No |
| **Extra infrastructure** | Required | None if already in stack | None (uses existing K8s API) | None | None |
| **Lock performance** | Best | Good | Moderate | Good | Best |

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

Leader election uses a distributed lock to make sure that only one worker in the cluster acts as the leader at any given time.

The leader election service acquires and renews the distributed lock. It runs as an AnyIO task that you can start with the [Task Manager](./task.md#task-manager). The service runs in the background and renews the lock automatically so other workers cannot acquire it. The lock releases automatically when the task is cancelled or when the application shuts down.

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

- **Async**: the lock is acquired and released asynchronously.
- **Distributed**: the lock is shared across multiple workers.
- **Non-reentrant**: a nested acquire from the same task or thread raises `LockReentrantError`. Use separate instances if you need independent locks.
- **Idempotent backend**: the backend lets the same token re-acquire the lock, which extends the lease. Call `do_acquire` directly if you need to extend the lease explicitly.
- **Expiring**: the lock has a timeout that auto-releases the lock to prevent deadlocks.
- **Non-blocking**: lock operations do not block the async event loop.
- **Backend-agnostic**: several backends are supported, including Redis, PostgreSQL, and Kubernetes ConfigMap.


```python
--8<-- "sync/lock.py"
```

!!! warning
    The lock is designed for use within an async event loop and is not thread-safe or process-safe.

!!! tip "Want to understand how worker identity and lock tokens work internally?"
    See [Synchronization Internals](architecture/sync.md) for details on UUID generation, token scoping, and design guarantees.

### Configuration

The lock has two construction entry points, each one-purpose. The positional `name` is always required and acts as the instance identity.

=== "Programmatic"

    Pass fields directly as keyword arguments. Use this for scripts, notebooks, and code-first setups where every value is known inline.

    ```python
    --8<-- "sync/lock_programmatic.py"
    ```

=== "Environmental"

    Omit the kwargs and let the lock resolve fields from environment variables. Unset fields fall back to `LockConfig` defaults. The derived prefix is `GREL_LOCK_{NAME_UPPER}_`.

    ```python
    --8<-- "sync/lock_environmental.py"
    ```

=== "Declarative"

    Use `Lock.from_config(name, config)` to construct from a name and a pre-built `LockConfig`. The env path is bypassed entirely.

    ```python
    --8<-- "sync/lock_declarative.py"
    ```

### Environment variables

Prefix: `GREL_LOCK_{NAME_UPPER}_`

| Env var                                      | Config field     | Type            | Default          |
|----------------------------------------------|------------------|-----------------|------------------|
| `GREL_LOCK_{NAME_UPPER}_WORKER`              | `worker`         | `str \| UUID`   | generated UUID   |
| `GREL_LOCK_{NAME_UPPER}_LEASE_DURATION`      | `lease_duration` | `float` (> 0)   | `60`             |
| `GREL_LOCK_{NAME_UPPER}_RETRY_INTERVAL`      | `retry_interval` | `float` (≥ 0.001) | `0.1`          |

Concrete example for `Lock("cart")`:

```bash
GREL_LOCK_CART_WORKER=web-1
GREL_LOCK_CART_LEASE_DURATION=120
GREL_LOCK_CART_RETRY_INTERVAL=0.2
```

!!! tip "Override the env prefix"
    The derived prefix is only the zero-config default. Apps that want their own convention (for example `MYAPP_LOCK_CART_*`) pass `env_prefix=` explicitly. Pass `read_env=False` to skip env reading entirely when every field is already supplied via kwargs or when construction happens via `Lock.from_config(...)`.

!!! info "Composing with the wider settings tree"
    grelmicro does not ship a `BaseSettings` wrapper. Apps own the env namespace, the YAML path, and the aggregation strategy. Compose `LockConfig` into `pydantic-settings`, load it from YAML, secrets files, Vault, or any other source, then call `Lock.from_config("cart", cfg)`.

    See the [Configuration architecture](architecture/config.md) doc for the full resolution rules and the rationale behind the construction split.
