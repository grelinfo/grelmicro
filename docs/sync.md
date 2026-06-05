# Synchronization Primitives

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends (see more in the Backends section).

The available primitives are:

- **[Task Lock](#task-lock)**: A distributed lock for scheduled tasks with minimum and maximum hold times.
- **[Leader Election](#leader-election)**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **[Lock](#lock)**: A distributed lock that can be used to synchronize access to shared resources.

The synchronization primitives can be used in combination with the `Tasks` and `TaskRouter` to control task execution in a distributed system (see more in [Task Scheduler](task.md)).

## Quick start

Guard a shared resource with a distributed `Lock`. The Memory backend needs no extra service, so this runs as-is. Swap in Redis, Postgres, or Kubernetes for production:

```python
from grelmicro import Grelmicro
from grelmicro.sync import Lock, Sync
from grelmicro.sync.memory import MemorySyncAdapter

micro = Grelmicro(uses=[Sync(MemorySyncAdapter())])

lock = Lock("cart")


async def checkout():
    async with lock:
        ...
```

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

### Choosing a backend

Pick the backend that matches your deployment, not the fastest one on paper.

- **Memory**: use for tests and single-process apps. State lives in the process and disappears on restart. Never use it across nodes: each node holds its own locks and leader election is meaningless.
- **Redis**: use for distributed locks when you want the lowest latency. Acquire and renew are single Lua round-trips, so this is the fastest distributed option. Reach for it when lock throughput matters and you already run or can add Redis.
- **PostgreSQL**: use when Postgres is already in your stack. It needs no extra infrastructure and gives transactional, durable locks. Slightly slower than Redis, but the right default when you want one fewer moving part.
- **SQLite**: use for a single node that needs persistent locks with no operational overhead. State survives restarts on local disk, but it does not coordinate across nodes. Good for home labs and single-instance deployments.
- **Kubernetes**: use for leader election in a Kubernetes-native deployment. It builds on the Kubernetes Lease API and reuses the existing API server, so no extra infrastructure is needed. It guarantees one holder at a time within the configured lease, backed by etcd. It does not give you the low-latency, high-throughput locking of Redis: prefer it for coarse leader election, not for hot-path resource locks.

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

The leader election service acquires and renews the distributed lock. It runs as an asyncio task that you can start with the [Tasks](./task.md#tasks). The service runs in the background and renews the lock automatically so other workers cannot acquire it. The lock releases automatically when the task is cancelled or when the application shuts down.

=== "Tasks (Recommended)"
    ```python
    --8<-- "sync/leaderelection_task.py"
    ```

=== "asyncio Task Group (Advanced)"
    ```python
    --8<-- "sync/leaderelection_asyncio.py"
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
    The derived prefix is only the zero-config default. Apps that want their own convention (for example `MYAPP_LOCK_CART_*`) pass `env_prefix=` explicitly. Pass `env_load=False` to skip env reading entirely when every field is already supplied via kwargs or when construction happens via `Lock.from_config(...)`.

!!! info "Composing with the wider settings tree"
    grelmicro does not ship a `BaseSettings` wrapper. Apps own the env namespace, the YAML path, and the aggregation strategy. Compose `LockConfig` into `pydantic-settings`, load it from YAML, secrets files, Vault, or any other source, then call `Lock.from_config("cart", cfg)`.

    See the [Configuration architecture](architecture/config.md) doc for the full resolution rules and the rationale behind the construction split.

### Dynamic-key Locks

Most Locks are declared once at module load (`lock = Lock("cart")`) and reused across requests. When the lock key is computed per request, build a fresh `Lock` each time:

```python
lock = Lock(f"order:{order_id}")
async with lock:
    ...
```

This is the right pattern when locking by business identity (`order_id`, `user_id`, `tenant_id`).

#### Recommended: pre-build the config

Per-request `Lock(name)` re-runs `LockConfig` validation and the env path on every call. Pre-build a single `LockConfig` once, then call `Lock.from_config(name, cfg)` per request to skip both:

```python
from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig

ORDER_LOCK_CONFIG = LockConfig(lease_duration=30)

async def handle_order(order_id: int):
    lock = Lock.from_config(f"order:{order_id}", ORDER_LOCK_CONFIG)
    async with lock:
        await process_order(order_id)
```

`Lock.from_config(...)` accepts the same `backend=` argument as the constructor, so the dynamic-key Lock resolves the registered backend the same way a module-level Lock does.

#### Cost trade-off

| Construction path | Per call | Notes |
|---|---:|---|
| `Lock(name)` (programmatic, env disabled) | ~10 µs | Pydantic validation plus `env_segment(name)` for the default prefix |
| `Lock(name)` (env enabled, `GREL_ENV_LOAD=true` or `env_load=True`) | ~70 µs | Adds the env read on top |
| `Lock.from_config(name, cfg)` | ~10 µs | Skips env and the default-prefix build, reuses `cfg` |
| `async with lock` resolution | ~80 ns | `ContextVar.get` plus dict lookup |
| `backend.acquire(...)` (Redis Lua eval) | ~1 ms | Network round-trip |

The acquire round-trip dominates wall-clock. The construction cost matters only for high-throughput dynamic-key flows. `Lock.from_config(...)` keeps the construction cost flat regardless of the global `GREL_ENV_LOAD` setting.

#### When the simpler form is enough

A handful of dynamic-key Locks per request, on a handler that already pays a database round-trip, can keep using `Lock(name)` directly. Reach for `Lock.from_config(name, cfg)` when:

- the handler runs many Locks per request
- the path is on a measured hot loop
- the deployment has `GREL_ENV_LOAD=true` and you want to skip the env path on per-request construction
