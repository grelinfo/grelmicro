# Coordination

The `coordination` package elects one worker in a cluster to act as the leader.
Use it to run a job at most once across all replicas: a scheduler, a migration,
a compaction.

**Why**

- Run a task on exactly one worker without an external scheduler.
- Pick a backend for leader election independently from your `Lock` backend.
- Read who leads, since when, and the metadata they attached.
- Hand over leadership automatically when the leader stops or its lease expires.

## Quick start

Register a `Coordination` component, build a `LeaderElection`, and gate a task on
it. The Memory backend needs no extra service, so this runs as-is:

```python
--8<-- "coordination/quickstart.py"
```

Only the leader runs `run_once_in_the_cluster`. Every other worker skips it until
it becomes the leader.

## Independent backend

Leader election is **not** a `Lock`. A `Lock` is short-lived mutual exclusion. A
leader election is a long-lived role: "am I currently the leader?" The two answer
different questions and often want different backends.

Because `Coordination` is a separate component from `Sync`, you choose its
backend on its own. A service can keep `Lock` on Redis for low-latency mutual
exclusion and run leader election on a Kubernetes Lease, native to the cluster
and visible with `kubectl`:

```python
--8<-- "coordination/independent_backends.py"
```

## The lease record

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

## Backends

Pick the backend that matches your deployment.

| Backend | Use when | Stores the record in |
|---|---|---|
| `MemoryLeaderElectionBackend` | Tests and single-process apps. | A process-local dict (not shared across nodes). |
| `RedisLeaderElectionBackend` | A Redis-backed cluster. | A Redis hash, updated atomically with a Lua script. |
| `PostgresLeaderElectionBackend` | Postgres is already in your stack. | A row, updated atomically under an advisory lock. |
| `KubernetesLeaderElectionBackend` | A Kubernetes-native deployment. | A `coordination.k8s.io` Lease, metadata in its annotations. |

A `Provider` builds the matching backend for you: `Coordination(redis)` calls
`redis.leader_election()`. Pass a backend instance directly when it has no
provider, like the Kubernetes Lease.

!!! tip "Install"
    Redis needs the `redis` extra, Postgres the `postgres` extra, and Kubernetes
    the `kubernetes` extra. See the [installation guide](installation.md).

## Running without a component

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

## Configuration

`LeaderElection` follows the three-paths configuration contract. The lease timing
fields (`lease_duration`, `renew_deadline`, `retry_interval`, `backend_timeout`,
`error_interval`) resolve programmatically, from `GREL_LEADER_ELECTION_{NAME}_*`
environment variables, or from a pre-built `LeaderElectionConfig`. See
[Configuration](config.md) for the shared rules.

## Live reconfiguration

`LeaderElection` inherits `Reconfigurable[LeaderElectionConfig]`. Calling
`reconfigure(new_config)` swaps the timing for the next renew loop iteration. The
`worker` identity cannot change, since the lease is held under that token. See
[Live reconfiguration](architecture/reconfigure.md).

## Reference

See the [API reference](reference/coordination.md) for every option.
