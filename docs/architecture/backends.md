# Backends and Adapters

grelmicro splits infrastructure code into a small set of object kinds so each one stays swappable.

## Why async

Every backend uses **async** methods because it performs network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async keeps the event loop free during round-trips, which matters in microservice applications that handle many concurrent requests. Even backends with low-latency I/O (like SQLite) use async so the interface stays uniform and the loop can schedule other work during I/O waits.

## The kinds

| Kind | Examples | Role |
|---|---|---|
| **Provider** | `RedisProvider`, `PostgresProvider` | Owns the connection pool and the vendor config. Components attach to it. |
| **Component** | `Coordination`, `Cache`, `RateLimiters`, `CircuitBreakers` | Registration on a `Grelmicro` app: `(kind, name)` pair plus lifecycle. Accepts a Provider or a Backend. |
| **Backend** | `LockBackend`, `CacheBackend` (Protocol) | Pure interface. Memory backends (`MemoryLockAdapter`) implement it directly. |
| **Adapter** | `RedisLockAdapter`, `RedisCacheAdapter` | Internal. Built by `Provider.{kind}()` factory. Public escape hatch for custom Providers. |
| **Pattern** | `Lock`, `TaskLock`, `LeaderElection`, `TTLCache` | The user-facing primitive. Declared at module load, resolves its backend at use time. |

Users construct **Providers**, attach **Components** that share each Provider, and import **Patterns** at module level. Adapter classes rarely appear in user code.

## Distribution model

Not every Pattern needs a backend, and the ones that do not all need the same kind. Three tiers decide whether you register a Component at all.

**Distributed-mandatory.** `Lock`, `TaskLock`, `LeaderElection`, and the `@cron` schedule only mean something against a shared store: a lock that is local to one process is not a lock. They have no local fallback, so using one without a registered `Coordination` backend raises. Because every coordination Pattern runs on the same store, one `Coordination(provider)` wires the whole domain at once.

**Local-first, shared-optional.** `CircuitBreaker` and `RateLimiter` work per process out of the box and fall back to an in-process backend when nothing is registered. A circuit breaker is local by default on purpose: each replica trips on the failures it sees. Sharing is a deliberate opt-in, and the safe default differs per Pattern, so each has its own Component (`CircuitBreakers`, `RateLimiters`). Register `CircuitBreakers(redis)` only when one replica's open circuit should short-circuit the fleet, and `RateLimiters(redis)` only when the quota is global. Keeping them separate means opting rate limiting into a shared store does not also distribute your circuit breakers.

**Purely local.** `Retry`, `Timeout`, `Bulkhead`, `Shield`, and `Fallback` hold no shared state and never take a backend. Construct and use them directly.

This is why `Coordination` is a single component for its whole domain while resilience exposes one component per shared Pattern: coordination has one mandatory backend decision, resilience has independent per-Pattern ones.

## Construction vs registration

Construction and registration are two distinct steps. `__init__` validates configuration and binds locals. It performs no registry writes and no I/O. Registration happens when the Component is attached to a `Grelmicro` app.

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost")

micro = Grelmicro(uses=[
    redis,
    Coordination(redis),
    Cache(redis),
])

async with micro:
    # the provider is open, every component is open
    ...
# every item is closed on exit (LIFO)
```

`Coordination(provider)` calls `provider.lock()` to obtain the matching `LockBackend` and `provider.leaderelection()` for the `LeaderElectionBackend`. `Cache(provider)` calls `provider.cache()`. Memory backends bypass the Provider step: pass the adapter directly (`Coordination(lock=MemoryLockAdapter())`).

## Named backends and per-call selection

Register multiple Components under different names and pick one at the call site:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()
postgres = PostgresProvider()

micro = Grelmicro(uses=[
    redis,
    postgres,
    Coordination(redis),
    Coordination(postgres, name="analytics"),
])

Lock("cart")                       # → "default" (Redis)
Lock("audit", backend="analytics") # → "analytics" (Postgres)
Lock("cart", backend=my_adapter)   # → explicit instance, bypasses names
```

Resolution order, in priority:

1. Explicit instance (`backend=instance`).
2. The Component registered under `("coordination", requested_name)`.
3. When the requested name is `"default"` and exactly one Component of that kind is registered: that sole entry.
4. Otherwise raise `ComponentNotRegisteredError`.

## Test-time overrides

`micro.override(...)` installs scoped Component swaps for the duration of a block:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()
micro = Grelmicro(uses=[redis, Coordination(redis)])
lock = Lock("cart")

async with micro:
    async with micro.override(Coordination(lock=MemoryLockAdapter())):
        async with lock:  # routed to MemoryLockAdapter
            ...
```

The override propagates downward through `await`, `asyncio.create_task`, and `asyncio.to_thread` because asyncio copies the calling context at every concurrency boundary.

## Pure construction with explicit pass-through

Skip the app entirely for one-off usage:

```python
async with RedisProvider() as redis:
    lock = Lock(name="my-lock", backend=redis.lock())
    async with lock:
        ...
```

`async with` opens the connection only. The adapter is not registered with any app.

## Protocol-based polymorphism

Backends are defined by protocols (structural typing), not base classes. Any object implementing the required methods works. This enables:

- Swapping adapters without changing application code.
- Writing test adapters (e.g. `MemoryLockAdapter`) with no external dependencies.
- Adding new adapters without modifying existing code.

## Connection pool isolation

Components share a connection pool through a `Provider`: pass the same `RedisProvider` to two Components (`Coordination(redis)`, `Cache(redis)`) and they share one pool. To isolate pools, build distinct Providers with different `env_prefix=` values (`CACHE_REDIS_`, `SESSION_REDIS_`) and pass each to the matching Component.

The default behavior is **share when possible, isolate when asked**. Distinct Providers opt into per-domain isolation.

## Error handling

Accessing a Component that has not been registered raises `ComponentNotRegisteredError` with a descriptive message. Resolving a Pattern outside any `async with micro:` block raises `NoActiveAppError`.
