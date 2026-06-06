# Capability matrix

Which Pattern × Adapter combinations ship today, and which gaps remain.

The [roadmap](https://github.com/grelinfo/grelmicro/issues/124) carries the live state. This page is the at-a-glance view.

## Vocabulary

- **Pattern**: user-facing class. `Lock`, `LeaderElection`, `TaskLock`, `TTLCache`, `RateLimiter`, `CircuitBreaker`, `Retry`, `Bulkhead`, `Fallback`, `Timeout`.
- **Adapter**: concrete implementation of a Backend Protocol. `RedisSyncAdapter`, `PostgresSyncAdapter`, `MemoryCacheAdapter`, `SQLiteSyncAdapter`, `KubernetesSyncAdapter`, and so on.
- **Backend**: the Protocol class an Adapter satisfies. `SyncBackend`, `CacheBackend`, `RateLimiterBackend`, `CircuitBreakerBackend`.
- **Provider**: vendor configuration plus native client, shared by Adapters that talk to the same service. `RedisProvider`, `PostgresProvider`, `SQLiteProvider`. Memory and Kubernetes Adapters do not use a Provider.

See [Backends and Adapters](architecture/backends.md) for the full model.

## Matrix

| Pattern             | Memory                                                         | Redis                                                          | Postgres                                                       | SQLite                                                         | Kubernetes |
| ------------------- | :------------------------------------------------------------: | :------------------------------------------------------------: | :------------------------------------------------------------: | :------------------------------------------------------------: | :--------: |
| `Lock`              | ✅                                                             | ✅                                                             | ✅                                                             | ✅                                                             | ✅         |
| `TaskLock`          | ✅                                                             | ✅                                                             | ✅                                                             | ✅                                                             | ✅         |
| `LeaderElection`    | ✅                                                             | ✅                                                             | ✅                                                             | ✅                                                             | ✅         |
| `TTLCache`          | ✅                                                             | ✅                                                             | ✅                                                             | Future                                                         | N/A        |
| `RateLimiter`       | ✅                                                             | ✅                                                             | ✅                                                             | ✅                                                             | N/A        |
| `CircuitBreaker`    | ✅                                                             | ✅                                                             | ✅                                                             | Future                                                         | N/A        |
| `Retry`             | ✅                                                             | N/A                                                            | N/A                                                            | N/A                                                            | N/A        |
| `Bulkhead`          | ✅                                                             | N/A                                                            | N/A                                                            | N/A                                                            | N/A        |
| `Fallback`          | ✅                                                             | N/A                                                            | N/A                                                            | N/A                                                            | N/A        |
| `Timeout`           | ✅                                                             | N/A                                                            | N/A                                                            | N/A                                                            | N/A        |

Legend:

- ✅ ships today.
- `Future` planned for a later release.
- `N/A` does not apply. `Retry`, `Bulkhead`, `Fallback`, and `Timeout` are in-process Patterns with no remote state to share.

## Picking an Adapter

- **Memory** for tests, single-process apps, and `Retry`, `Fallback`, `Timeout`, and `Bulkhead` (in-process Patterns).
- **Redis** when you already run Redis and want the lowest-latency distributed option.
- **Postgres** when Postgres is your only stateful dependency and you want one fewer service to run.
- **SQLite** for single-host deployments that still need durability across restarts.
- **Kubernetes** for `Lock` and `LeaderElection` when you want the cluster API as the coordination plane and no extra infrastructure.
