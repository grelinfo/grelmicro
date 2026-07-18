# Capability matrix

Which Pattern × Adapter combinations ship today, and which gaps remain.

The [roadmap](https://github.com/grelinfo/grelmicro/issues/124) carries the live state. This page is the at-a-glance view.

## Vocabulary

- **Pattern**: user-facing class. `Lock`, `LeaderElection`, `TaskLock`, `TTLCache`, `RateLimiter`, `CircuitBreaker`, `Idempotency`, `Outbox`, `Retry`, `Bulkhead`, `Fallback`, `Timeout`, `Shield`.
- **Adapter**: concrete implementation of a Backend Protocol. `RedisLockAdapter`, `PostgresLockAdapter`, `MemoryCacheAdapter`, `PostgresOutboxAdapter`, `SQLiteLockAdapter`, `KubernetesLockAdapter`, and so on.
- **Backend**: the Protocol class an Adapter satisfies. `LockBackend`, `LeaderElectionBackend`, `CacheBackend`, `RateLimiterBackend`, `CircuitBreakerBackend`, `OutboxBackend`.
- **Provider**: vendor configuration plus native client, shared by Adapters that talk to the same service. `RedisProvider`, `PostgresProvider`, `SQLiteProvider`. Memory and Kubernetes Adapters do not use a Provider.

See [Backends and Adapters](architecture/backends.md) for the full model.

## Matrix

| Pattern | Memory | Redis | Postgres | SQLite | Kubernetes |
| --- | :---: | :---: | :---: | :---: | :---: |
| `Lock` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `TaskLock` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `LeaderElection` | ✅ | ✅ | ✅ | N/A | ✅ |
| `@tasks.cron` | ✅ | ✅ | ✅ | ✅ | N/A |
| `TTLCache` | ✅ | ✅ | ✅ | ✅ | N/A |
| `Idempotency` | ✅ | ✅ | ✅ | ✅ | N/A |
| `Outbox` | ✅ | N/A | ✅ | 🚧 | N/A |
| `RateLimiter` | ✅ | ✅ | ✅ | ✅ | N/A |
| `CircuitBreaker` | ✅ | ✅ | ✅ | ✅ | N/A |
| `Retry` | ✅ | N/A | N/A | N/A | N/A |
| `Bulkhead` | ✅ | N/A | N/A | N/A | N/A |
| `Fallback` | ✅ | N/A | N/A | N/A | N/A |
| `Timeout` | ✅ | N/A | N/A | N/A | N/A |
| `Shield` | ✅ | N/A | N/A | N/A | N/A |

Legend:

- ✅ ships today.
- 🚧 planned (see the [roadmap](https://github.com/grelinfo/grelmicro/issues/124)).
- `N/A` does not apply. `Retry`, `Bulkhead`, `Fallback`, and `Timeout` are in-process Patterns with no remote state to share. For `Schedule` (cron), Kubernetes has no Adapter on purpose: run a native [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) instead. For `LeaderElection`, SQLite has no adapter: leader election is meaningful only across multiple nodes, and SQLite does not coordinate across nodes. `Schedule` (cron) on SQLite does ship, because durable cron across processes on a single host is still useful. For `Outbox`, Redis and Kubernetes are `N/A`: the outbox stages a message in the same transaction as your business write, which needs a transactional SQL store (Memory ships for tests and single-process apps). The Postgres adapter ships today, SQLite is planned, and MySQL is on the roadmap.

## Picking an Adapter

- **Memory** for tests, single-process apps, and `Retry`, `Fallback`, `Timeout`, and `Bulkhead` (in-process Patterns).
- **Redis** when you already run Redis and want the lowest-latency distributed option. `RedisProvider` switches to Sentinel or Cluster from the URL scheme alone (see [Providers](providers.md)). On Cluster the cache and lock prefixes need a hash tag.
- **Valkey** when you run a Valkey server. `ValkeyProvider` wraps the same Redis adapters via `valkey-py`, so coverage and behavior match Redis exactly.
- **Postgres** when Postgres is your only stateful dependency and you want one fewer service to run.
- **SQLite** for single-host deployments that still need durability across restarts. The SQLite circuit breaker coordinates state across processes sharing one file on a single host, not across hosts. For fleet-wide state, use Redis or Postgres.
- **Kubernetes** for `Lock` and `LeaderElection` when you want the cluster API as the coordination plane and no extra infrastructure.
