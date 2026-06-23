# Coordination

The `coordination` package gives you distributed `Lock`, `TaskLock`, and
`LeaderElection`: the primitives that keep work correct when your service runs as
many replicas.

- **[Lock](#lock)**: mutual exclusion across workers. Hold a shared resource one
  caller at a time.
- **[Task Lock](#task-lock)**: a lock for scheduled tasks. It holds long enough to
  stop another worker re-running the same tick.
- **[Leader Election](#leader-election)**: elect one worker to play a long-lived
  role. Run a job at most once across all replicas.

All three are technology agnostic and run on the same backends (see
[Backends](#backends)). Pick Redis, PostgreSQL, SQLite, Kubernetes, or in-memory
without changing your code.

Use them together with `Tasks` and `TaskRouter` to control task execution across
a cluster (see the [Task Scheduler](task.md)).

!!! warning "Thread safety"
    The primitives are built for one async event loop and are **not
    thread-safe**. Sync access from worker threads goes through `from_thread`
    adapters, which dispatch operations to the event loop. Do not share instances
    across event loops or threads without the adapter.

## Quick start

Guard a shared resource with a distributed `Lock`. The Memory backend needs no
extra service, so this runs as-is. Swap in Redis, Postgres, SQLite, or Kubernetes
for production:

```python
--8<-- "coordination/quickstart_lock.py"
```

One caller holds `cart` at a time. The next caller waits for the release.

## Backends

Load a backend before using any primitive. A `Coordination` component wraps the
backends and resolves them for you.

!!! tip "Install"
    Each backend needs its own extra:

    - Redis: `pip install "grelmicro[redis]"`
    - PostgreSQL: `pip install "grelmicro[postgres]"`
    - SQLite: `pip install "grelmicro[sqlite]"`
    - Kubernetes: `pip install "grelmicro[kubernetes]"`

    See the [installation guide](installation.md) for `uv` and `poetry`.

Wire a `Coordination` component like this:

=== "Redis"
    ```python
    --8<-- "coordination/redis.py"
    ```

=== "Postgres"
    ```python
    --8<-- "coordination/postgres.py"
    ```

=== "Kubernetes"
    ```python
    --8<-- "coordination/kubernetes.py"
    ```

=== "SQLite"
    ```python
    --8<-- "coordination/sqlite.py"
    ```

=== "Memory"
    ```python
    --8<-- "coordination/memory.py"
    ```

!!! warning
    Store connection URLs in a proper place, such as environment variables, not
    inline like the examples above.

A `Provider` resolves both primitives in one line: `Coordination(redis)` calls
`redis.lock()` for the lock backend and `redis.leaderelection()` for the
election backend. Set each backend on its own with `lock=` and `election=`, so
locks can run on one vendor and leader election on another. Each argument accepts
a `Provider`, a backend instance, or a zero-arg class.

| | Redis | PostgreSQL | Kubernetes | SQLite | Memory |
|---|---|---|---|---|---|
| **Use case** | Production | Production | Production (K8s-native) | Home lab / Local testing | Testing only |
| **Multi-node** | Yes | Yes | Yes | No | No |
| **Persistence** | Yes | Yes | Yes (etcd-backed) | Yes | No |
| **Extra infrastructure** | Required | None if already in stack | None (uses existing K8s API) | None | None |
| **Lock performance** | Best | Good | Moderate | Good | Best |

!!! tip
    Feel free to create your own backend and contribute it. The backend
    protocols (`LockBackend`, `LeaderElectionBackend`, `ScheduleBackend`) are
    exported from `grelmicro.coordination`.

### Choosing a backend

Pick the backend that matches your deployment, not the fastest one on paper.

- **Memory**: use for tests and single-process apps. State lives in the process
  and disappears on restart. Never use it across nodes: each node holds its own
  locks and leader election is meaningless.
- **Redis**: use for distributed locks when you want the lowest latency. Acquire
  and renew are single round-trips, so this is the fastest distributed option.
  Reach for it when lock throughput matters and you already run or can add Redis.
- **PostgreSQL**: use when Postgres is already in your stack. It needs no extra
  infrastructure and gives transactional, durable locks. Slightly slower than
  Redis, but the right default when you want one fewer moving part.
- **SQLite**: use for a single node that needs persistent locks with no
  operational overhead. State survives restarts on local disk, but it does not
  coordinate across nodes. Good for home labs and single-instance deployments.
- **Kubernetes**: use for leader election in a Kubernetes-native deployment. It
  builds on the Kubernetes Lease API and reuses the existing API server, so no
  extra infrastructure is needed. It guarantees one holder at a time within the
  configured lease, backed by etcd. It does not give you the low-latency,
  high-throughput locking of Redis: prefer it for coarse leader election, not for
  hot-path resource locks.

## Lock

The lock is a distributed lock that synchronizes access to a shared resource.

The lock supports the following features:

- **Async**: the lock is acquired and released asynchronously.
- **Distributed**: the lock is shared across multiple workers.
- **Non-reentrant**: a nested acquire from the same task or thread raises
  `LockReentrantError`. Use separate instances if you need independent locks.
- **Idempotent backend**: the backend lets the same token re-acquire the lock,
  which extends the lease. Call `extend()` if you need to extend the
  lease explicitly.
- **Expiring**: the lock has a timeout that auto-releases the lock to prevent
  deadlocks.
- **Non-blocking**: lock operations do not block the async event loop.
- **Backend-agnostic**: several backends are supported, including Redis,
  PostgreSQL, and Kubernetes ConfigMap.

```python
--8<-- "coordination/lock.py"
```

!!! warning
    The lock is built for one async event loop and is not thread-safe or
    process-safe.

!!! tip "Want to understand how worker identity and lock tokens work internally?"
    See [Coordination Internals](architecture/coordination.md) for details on
    UUID generation, token scoping, and design guarantees.

### Configuration

Build the lock with keyword arguments. The positional `name` is always required
and acts as the instance identity.

```python
--8<-- "coordination/lock_programmatic.py"
```

### Environment variables

Tune any field in deployment without code changes.

Prefix: `GREL_LOCK_{NAME_UPPER}_`. The default instance drops the name segment and reads `GREL_LOCK_*`.

| Env var                                      | Config field     | Type            | Default          |
|----------------------------------------------|------------------|-----------------|------------------|
| `GREL_LOCK_{NAME_UPPER}_WORKER`              | `worker`         | `str \| UUID`   | generated UUID   |
| `GREL_LOCK_{NAME_UPPER}_LEASE_DURATION`      | `lease_duration` | `float` (> 0)   | `60`             |
| `GREL_LOCK_{NAME_UPPER}_RETRY_INTERVAL`      | `retry_interval` | `float` (>= 0.001) | `0.1`         |
| `GREL_LOCK_{NAME_UPPER}_RETRY_JITTER`        | `retry_jitter`   | `float` [0, 1)     | `0.1`         |

Concrete example for `Lock("cart")`:

```bash
GREL_LOCK_CART_WORKER=web-1
GREL_LOCK_CART_LEASE_DURATION=120
GREL_LOCK_CART_RETRY_INTERVAL=0.2
GREL_LOCK_CART_RETRY_JITTER=0.2
```

!!! tip "Advanced"
    For custom env prefixes with `env_prefix=`, the `from_config` declarative
    path, and `pydantic-settings` composition, see
    [Declarative configuration](advanced/config.md).

### Dynamic-key Locks

Most Locks are declared once at module load (`lock = Lock("cart")`) and reused
across requests. When the lock key is computed per request, build a fresh `Lock`
each time:

```python
lock = Lock(f"order:{order_id}")
async with lock:
    ...
```

This is the right pattern when locking by business identity (`order_id`,
`user_id`, `tenant_id`).

!!! tip "Advanced"
    On a measured hot loop that builds many Locks per request, pre-build a single
    `LockConfig` and call `Lock.from_config(name, cfg)` to skip per-call
    validation and the env read. See
    [Declarative configuration](advanced/config.md).

### Bounded acquire

Pass `timeout=` to `acquire()` to limit how long the call waits. When the
deadline passes without winning the lock, `TimeoutError` is raised:

```python
# Wait up to 5 seconds, then raise TimeoutError.
held = await lock.acquire(timeout=5.0)
```

The context manager (`async with lock`) calls `acquire()` with no timeout and
waits indefinitely. Use `acquire(timeout=...)` directly when you need a
bounded wait and want to handle the failure yourself.

### Extending the lease

Call `extend()` on a `Lock` to renew the TTL without releasing the lock. The
fencing token stays the same, only the expiry time advances:

```python
lock = Lock("cart")
async with lock as held:
    token_before = held.fencing_token
    extended = await lock.extend()
    assert extended.fencing_token == token_before  # same token, new TTL
```

`extend()` raises `LockNotOwnedError` when the lease was lost on the backend
(expired or taken over by another holder).

### Fencing tokens

A fencing token is a strictly increasing integer the backend mints for a lock
name. Each acquisition returns a `LockHandle` that carries it. Read it from the
value the context manager binds:

```python
async with Lock("cart") as held:
    print(held.fencing_token)
```

The token grows by one on every free-to-held transition: a new holder, or a
takeover after the previous lease expired. It keeps climbing across release and
re-acquire cycles, so a token is never reused for a name. The same holder
renewing or extending its lease keeps the same token.

`acquire()` and `acquire_nowait()` also return the `LockHandle`. The handle is
per-acquisition, so a `Lock` shared by several tasks gives each holder its own
handle with its own token.

Every backend mints tokens that are strictly monotonic per name. Redis is
strictly monotonic against its master.

!!! warning "The resource enforces, grelmicro mints"
    A fencing token only protects a resource that checks it. grelmicro hands you
    the token. The resource you write to must record the highest token it has
    accepted and reject any write that arrives with a lower or equal token.
    Without that check on the resource, a paused or partitioned old holder can
    still write after a new holder took over.

    The pattern: read `held.fencing_token`, pass it to the resource on every
    write, and have the resource compare it against its stored high-water mark.

```python
--8<-- "coordination/fencing.py"
```

## Task Lock

The Task Lock is a distributed lock for scheduled tasks. Unlike a regular Lock,
it does not release immediately. It keeps the lock held for a configurable
minimum duration to stop re-execution on other nodes.

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
    For scheduled tasks, prefer the
    [`interval()` decorator with `lock=TaskLock(...)`](task.md#distributed-lock),
    which re-stamps the lock with the task name automatically.

!!! warning
    When the lock expires before the task completes (`lease_duration`
    exceeded), another node may acquire the lock and execute concurrently. A
    warning is logged in this case.

## Leader Election

Leader election elects one worker in a cluster to act as the leader. Use it to
run a job at most once across all replicas: a scheduler, a migration, a
compaction.

- Run a task on exactly one worker without an external scheduler.
- Pick a backend for leader election independently from your `Lock` backend.
- Read who leads, since when, and the metadata they attached.
- Hand over leadership automatically when the leader stops or its lease expires.

### Quick start

Register a `Coordination` component, build a `LeaderElection`, and gate a task on
it. The Memory backend needs no extra service, so this runs as-is:

```python
--8<-- "coordination/quickstart.py"
```

Only the leader runs `run_once_in_the_cluster`. Every other worker skips it until
it becomes the leader.

### Independent backend

Leader election is **not** a `Lock`. A `Lock` is short-lived mutual exclusion. A
leader election is a long-lived role: "am I currently the leader?" The two answer
different questions and often want different backends.

A `Coordination` component sets each backend on its own. A service can keep
`Lock` on Redis for low-latency mutual exclusion and run leader election on a
Kubernetes Lease, native to the cluster and visible with `kubectl`:

```python
--8<-- "coordination/independent_backends.py"
```

### The lease record

Unlike a lock token, a leader election lease carries state. Every worker can read
the current `LeaderRecord` through `LeaderElection.record`: who holds the lease,
when they acquired and last renewed it, how many times leadership has changed
hands, and any metadata the holder attached. The shape follows the Kubernetes
`LeaderElectionRecord`.

```python
--8<-- "coordination/metadata.py"
```

`record` is `None` until the first acquire/renew completes, then updates on every
renew loop iteration.

### Leader election backends

Pick the backend that matches your deployment.

| Backend | Use when | Stores the record in |
|---|---|---|
| `MemoryLeaderElectionAdapter` | Tests and single-process apps. | A process-local dict (not shared across nodes). |
| `RedisLeaderElectionAdapter` | A Redis-backed cluster. | A Redis hash, updated atomically. |
| `PostgresLeaderElectionAdapter` | Postgres is already in your stack. | A row, updated atomically under an advisory lock. |
| `KubernetesLeaderElectionAdapter` | A Kubernetes-native deployment. | A `coordination.k8s.io` Lease, metadata in its annotations. |

A `Provider` builds the matching backend for you: `Coordination(redis)` calls
`redis.leaderelection()`. Pass a backend instance directly when it has no
provider, like the Kubernetes Lease.

### Running without a component

`LeaderElection` is a `Task`. Register it with `Tasks` (recommended), or drive it
directly inside an `asyncio.TaskGroup`:

=== "Tasks (recommended)"
    ```python
    --8<-- "coordination/leaderelection_task.py"
    ```

=== "asyncio Task Group (advanced)"
    ```python
    --8<-- "coordination/leaderelection_asyncio.py"
    ```

### Configuration

Build `LeaderElection` with keyword arguments. The lease timing fields
(`lease_duration`, `renew_deadline`, `retry_interval`, `retry_jitter`,
`backend_timeout`, `error_interval`) tune in deployment from
`GREL_LEADERELECTION_{NAME_UPPER}_*` environment variables. See
[Configuration](config.md) for the deployment story.

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition,
    see [Declarative configuration](advanced/config.md).

### Live reconfiguration

`LeaderElection` inherits `Reconfigurable[LeaderElectionConfig]`. Calling
`reconfigure(new_config)` swaps the timing for the next renew loop iteration. The
`worker` identity cannot change, since the lease is held under that token. See
[Live reconfiguration](architecture/reconfigure.md).

## Reference

See the [API reference](reference/coordination.md) for every option.
