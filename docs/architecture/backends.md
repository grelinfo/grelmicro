# Backends and Adapters

grelmicro splits infrastructure code into a small set of object kinds so each one stays swappable.

## Why async

Every backend uses **async** methods because it performs network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async keeps the event loop free during round-trips, which matters in microservice applications that handle many concurrent requests. Even backends with low-latency I/O (like SQLite) use async so the interface stays uniform and the loop can schedule other work during I/O waits.

## The kinds

| Kind | Examples | Role |
|---|---|---|
| **Provider** | `RedisProvider`, `PostgresProvider` | Owns the connection pool and the vendor config. Components attach to it. |
| **Component** | `Coordination`, `Cache`, `RateLimiterRegistry`, `CircuitBreakerRegistry` | Registration on a `Grelmicro` app: `(kind, name)` pair plus lifecycle. Accepts a Provider or a Backend. |
| **Backend** | `LockBackend`, `CacheBackend` (Protocol) | Pure interface. Memory backends (`MemoryLockAdapter`) implement it directly. |
| **Adapter** | `RedisLockAdapter`, `RedisCacheAdapter` | Internal. Built by `Provider.{kind}()` factory. Public escape hatch for custom Providers. |
| **Pattern** | `Lock`, `TaskLock`, `LeaderElection`, `TTLCache` | The user-facing primitive. Declared at module load, resolves its backend at use time. |

Users construct **Providers**, attach **Components** that share each Provider, and import **Patterns** at module level. Adapter classes rarely appear in user code.

## Distribution model

Not every Pattern needs a backend, and the ones that do behave differently when one is missing. What a Pattern does without a registered backend sorts it into three tiers.

**Backend required.** `Lock`, `TaskLock`, and `LeaderElection` only mean something against a shared store: a lock local to one process is not a lock. They have no safe local fallback, so using one without a registered `Coordination` backend raises.

**Backend optional, degrades safely.** `CircuitBreaker`, `RateLimiter`, and the `@cron` schedule all run without a backend, they just stop coordinating across replicas. A circuit breaker trips per replica, a rate limiter counts per replica, and a `@cron` task runs on every worker instead of once across the fleet. Sharing is a deliberate opt-in, and the safe default differs per Pattern: a circuit breaker is best left local (each replica reacts to what it sees), a rate limiter is often global, and a cron task usually wants the schedule backend so it fires once. Resilience keeps `CircuitBreakerRegistry` and `RateLimiterRegistry` as separate Components so opting rate limiting into a shared store does not also distribute your circuit breakers.

**Purely local.** `Retry`, `Timeout`, `Bulkhead`, `Shield`, and `Fallback` hold no shared state and never take a backend. Construct and use them directly.

`Coordination` groups the coordination backends (lock, leader election, schedule) under one Component because they belong to one domain, though you can still wire each to a different backend (`Coordination(lock=..., election=..., schedule=...)`). Resilience instead exposes one Component per shared Pattern, because each carries an independent sharing decision.

## Construction vs registration

Construction and registration are two distinct steps. `__init__` validates configuration and binds locals. It performs no registry writes and no I/O. Registration happens when the Component is attached to a `Grelmicro` app.

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost")

micro = Grelmicro(uses=[
    Coordination(redis),
    Cache(redis),
])

async with micro:
    # the provider is open, every component is open
    ...
# every item is closed on exit (LIFO)
```

`Coordination(provider)` calls `provider.lock()` to obtain the matching `LockBackend` and `provider.leaderelection()` for the `LeaderElectionBackend`. `Cache(provider)` calls `provider.cache()`. Memory backends bypass the Provider step: pass the adapter directly (`Coordination(lock=MemoryLockAdapter())`).

## Forgiving uses lists

`Grelmicro(uses=[...])` and `micro.use(...)` accept three shorthands so you write less ceremony. Each one expands to a precise explicit form. The explicit form is always legal and always wins.

**Bare Component class.** A Component class that constructs with no arguments is instantiated with its defaults. A class with a required argument (`Cache` needs its source) must be passed as an instance.

```python
Grelmicro(uses=[Tasks, HealthChecks])  # same as Grelmicro(uses=[Tasks(), HealthChecks()])
```

**Bare Adapter, class or instance.** An adapter is wrapped in its matching Component. A class is constructed first.

```python
Grelmicro(uses=[MemoryCacheAdapter])    # same as Cache(MemoryCacheAdapter())
Grelmicro(uses=[MemoryCacheAdapter()])  # same as Cache(MemoryCacheAdapter())
Grelmicro(uses=[MemoryLockAdapter()])   # wrapped in Coordination
```

The adapter kind decides the Component: a cache adapter becomes `Cache`, a lock adapter becomes `Coordination`, a rate limiter adapter becomes `RateLimiterRegistry`, a circuit breaker adapter becomes `CircuitBreakerRegistry`.

**Bare Provider.** A Provider listed with no Components registers one default Component for every kind it serves.

```python
redis = RedisProvider("redis://localhost")
Grelmicro(uses=[redis])
# registers Coordination, Cache, RateLimiterRegistry, CircuitBreakerRegistry, all on redis
```

A Provider serves a kind when its factory for that kind is implemented. `RedisProvider` and `PostgresProvider` serve all four kinds. `SQLiteProvider` skips leader election. An unserved kind (the factory raises `NotImplementedError`) is skipped. Any other factory error propagates, a misconfigured Provider fails loudly instead of registering a partial app.

This auto-registration is all-or-nothing. The moment you list **any** explicit Component, every Provider in the list becomes lifecycle-only and registers nothing. The list then reads the way it runs: a lone Provider is a full default app, a Provider beside Components is just a shared connection the Components wire themselves.

```python
# auto-registered: one default Component per served kind
Grelmicro(uses=[redis])

# explicit: redis is lifecycle-only, only Cache is registered
Grelmicro(uses=[Cache(redis)])
```

Two bare Providers with no Components raise `AmbiguousProviderError`: the default Component for a shared kind could come from either Provider. Wrap each Provider in the Components it should serve to resolve it.

```python
# raises AmbiguousProviderError
Grelmicro(uses=[redis, postgres])

# explicit and unambiguous
Grelmicro(uses=[Cache(redis), Coordination(postgres)])
```

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

## Request handlers and the ambient scope

Ambient resolution reads `Grelmicro.current()`, which is per asyncio task. A FastAPI request handler runs in its own task, outside the `async with micro:` block, so a bare `Lock("cart")` cannot see the app there and raises `OutOfContextError`. Add the middleware to extend the app scope to every request:

```python
from grelmicro.integrations.fastapi import GrelmicroMiddleware

app.add_middleware(GrelmicroMiddleware, micro=micro)
```

The middleware is pure ASGI, binds on `http` and `websocket` scopes, and works with any ASGI framework. Background `Tasks` already run inside the app scope and need nothing.

## Test-time overrides

`micro.override(...)` installs scoped Component swaps for the duration of a block:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()
micro = Grelmicro(uses=[Coordination(redis)])
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

A Provider is shared by identity: every Component that uses the same Provider attaches to the same connection pool. To break that sharing, build a distinct Provider with its own `env_prefix=`, pass a separate Provider instance, or wrap an externally owned client with `from_client`. There is no `shared=False` flag.

## Error handling

Accessing a Component that has not been registered raises `ComponentNotRegisteredError` with a descriptive message. Resolving a Pattern outside any `async with micro:` block raises `NoActiveAppError`.
