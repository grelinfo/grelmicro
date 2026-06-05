# Graceful shutdown

Container orchestrators (Kubernetes, ECS, systemd, Docker) stop a service by sending `SIGTERM` and then waiting a grace period (Kubernetes defaults to 30 seconds via `terminationGracePeriodSeconds`) before sending `SIGKILL`. A well-behaved service uses that window to:

1. Stop accepting new work.
2. Drain in-flight work.
3. Release distributed resources so other replicas do not wait for a lease to expire.

grelmicro stays library-shaped: the application owns the event loop and signal wiring. grelmicro gives you a clean way to translate a signal into shutdown and bounds how long draining takes.

## Wire the signal to shutdown

Translate `SIGTERM` and `SIGINT` into a future, then race it against the workload. Leaving the `Tasks` context drains the background tasks:

```python
--8<-- "task/graceful_shutdown.py"
```

When the host framework already owns the loop, let it do the wiring. Uvicorn and FastStream install their own signal handlers and exit the lifespan on `SIGTERM`, so a `Tasks` context entered inside the lifespan drains automatically. Add explicit handlers only for a plain `asyncio.run` or `uvloop.run` entry point.

## Draining background tasks

`Tasks(shutdown_timeout=...)` controls the drain. On exit, `Tasks` sets a stop signal that every `interval` task observes:

- A task that is sleeping between runs wakes immediately and exits.
- A task that is mid-run finishes its current iteration, then exits before the next one. In-flight work is never interrupted.
- A task still running after `shutdown_timeout` seconds is force-cancelled.

`shutdown_timeout` defaults to `30.0`, matching the Kubernetes grace period. Keep it at or below the pod `terminationGracePeriodSeconds` so draining finishes before `SIGKILL`. Set it to `0` to cancel immediately without draining.

The stop signal only bounds shutdown for tasks that are stuck mid-iteration. Cooperative tasks exit as soon as their current run completes, so a healthy service shuts down well within the window regardless of the timeout.

## Releasing distributed resources

| Primitive | On graceful stop |
|---|---|
| `LeaderElection` | Breaks its loop after the current renew and releases the lock on the backend, so a standby replica takes over without waiting for the lease to expire. |
| `Lock`, `TaskLock` | Released when the holder leaves `async with`. A task force-cancelled while holding the lease does not release it explicitly: the lease expires on the backend after its TTL. Keep `lease_duration` short enough that the worst-case takeover delay is acceptable. |
| `RateLimiter`, `CircuitBreaker`, `HealthChecks` | Stateless across requests, so they have no shutdown obligation. |

The lease-on-cancel contract is the reason to prefer cooperative draining: a task that finishes its iteration releases its locks through `async with`, while a force-cancel falls back to TTL expiry.
