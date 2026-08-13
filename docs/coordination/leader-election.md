# Leader Election

Leader election elects one worker in a cluster to act as the leader. Use it to
run a job at most once across all replicas: a scheduler, a migration, a
compaction.

- Run a task on exactly one worker without an external scheduler.
- Pick a backend for leader election independently from your `Lock` backend.
- Read who leads, since when, and the metadata they attached.
- Hand over leadership automatically when the leader stops or its lease expires.

## Quick start

Register a provider, build a `LeaderElection`, and gate a task on it:

```python
--8<-- "coordination/quickstart.py"
```

Only the leader runs `run_once_in_the_cluster`. Every other worker skips it until
it becomes the leader.

## Run only while leader

`@tasks.every(..., leader=leader)` gates each tick. When you have one long-lived
piece of work that should run for as long as you lead and stop the instant you do
not, use `lead`:

```python
--8<-- "coordination/lead.py"
```

`lead` waits for leadership, runs the coroutine in a child task, and **cancels it
the moment leadership is lost**, so no stale work outlives the lease. It returns
the result if the body finishes while still leader, or `None` if it was cancelled.
Pass `repeat=True` to re-run after re-acquiring leadership. Cancellation is
cooperative: it lands at the body's next `await`, so pair it with
`is_leader_confirmed_within` or a fencing token for writes that must never overlap
a successor. The `LeaderElection` service must be running concurrently (added to
`Tasks` above) to renew the lease and drive the leadership changes `lead` waits on.

## Independent backend

Leader election is **not** a [`Lock`](lock.md). A `Lock` is short-lived mutual
exclusion. A leader election is a long-lived role: "am I currently the leader?"
The two answer different questions and often want different backends.

A `Coordination` component sets each backend on its own. A service can keep
`Lock` on Redis for low-latency mutual exclusion and run leader election on a
Kubernetes Lease, native to the cluster and visible with `kubectl`:

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

## Leader election backends

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

Build `LeaderElection` with keyword arguments. The lease timing fields
(`lease_duration`, `renew_deadline`, `retry_interval`, `retry_jitter`,
`backend_timeout`, `error_interval`) tune in deployment from
`GREL_LEADERELECTION_{NAME_UPPER}_*` environment variables. See
[Configuration](../config.md) for the deployment story.

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition,
    see [Declarative configuration](../advanced/config.md).

## Live reconfiguration

`LeaderElection` inherits `Reconfigurable[LeaderElectionConfig]`. Calling
`reconfigure(new_config)` swaps the timing for the next renew loop iteration. The
`worker` identity cannot change, since the lease is held under that token. See
[Live reconfiguration](../architecture/reconfigure.md).
