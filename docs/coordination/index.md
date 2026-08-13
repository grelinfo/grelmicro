# Coordination

The `coordination` package gives you distributed `Lock`, `ReadWriteLock`,
`TaskLock`, and `LeaderElection`: the primitives that keep work correct when your
service runs as many replicas.

- **[Lock](lock.md)**: mutual exclusion across workers. Hold a shared resource one
  caller at a time.
- **[Read-Write Lock](read-write-lock.md)**: many readers at once, one writer
  alone. For a resource read far more often than it is written.
- **[Task Lock](task-lock.md)**: a lock for scheduled tasks. It holds long enough to
  stop another worker re-running the same tick.
- **[Leader Election](leader-election.md)**: elect one worker to play a long-lived
  role. Run a job at most once across all replicas.

All four are technology agnostic and run on the same backends (see
[Backends](#backends)). Pick Redis, PostgreSQL, SQLite, Kubernetes, or in-memory
without changing your code.

Use them together with `Tasks` and `TaskRouter` to control task execution across
a cluster (see the [Task Scheduler](../task.md)).

!!! warning "Thread safety"
    The primitives are built for one async event loop and are **not
    thread-safe**. Sync access from worker threads goes through `from_thread`
    adapters, which dispatch operations to the event loop. Do not share instances
    across event loops or threads without the adapter.

## Quick start

Guard a shared resource with a distributed `Lock`. One provider line says where
the lock state lives:

```python
--8<-- "coordination/quickstart_lock.py"
```

One caller holds `cart` at a time, on any worker. The next caller waits for the
release.

Redis needs the `redis` extra: `pip install "grelmicro[redis]"`. Postgres,
SQLite, and Kubernetes work the same way, see [Backends](#backends).

## Backends

Load a backend before using any primitive. A `Coordination` component wraps the
backends and resolves them for you.

!!! tip "Install"
    Each backend needs its own extra:

    - Redis: `pip install "grelmicro[redis]"`
    - PostgreSQL: `pip install "grelmicro[postgres]"`
    - SQLite: `pip install "grelmicro[sqlite]"`
    - Kubernetes: `pip install "grelmicro[kubernetes]"`

    See the [installation guide](../installation.md) for `uv` and `poetry`.

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

A `Provider` resolves every primitive in one line: `Coordination(redis)` calls
`redis.lock()` for the lock backend, `redis.readwritelock()` for the read-write
lock backend, and `redis.leaderelection()` for the election backend. Set each
backend on its own with `lock=`, `rwlock=`, and `election=`, so locks can run on
one vendor and leader election on another. Each argument accepts a `Provider`, a
backend instance, or a zero-arg class. See [Providers](../providers/index.md).

| | Redis | PostgreSQL | Kubernetes | SQLite | Memory |
|---|---|---|---|---|---|
| **Use case** | Production | Production | Production (K8s-native) | Home lab / Local testing | Testing only |
| **Multi-node** | Yes | Yes | Yes | No | No |
| **Persistence** | Yes | Yes | Yes (etcd-backed) | Yes | No |
| **Extra infrastructure** | Required | None if already in stack | None (uses existing K8s API) | None | None |
| **Lock performance** | Best | Good | Moderate | Good | Best |

!!! tip
    Feel free to create your own backend and contribute it. The backend
    protocols (`LockBackend`, `ReadWriteLockBackend`, `LeaderElectionBackend`,
    `ScheduleBackend`) are exported from `grelmicro.coordination`.

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

## Reference

See the [API reference](../reference/coordination.md) for every option.
