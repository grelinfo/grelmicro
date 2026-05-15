# Backends and Adapters

grelmicro splits infrastructure code into a small set of object kinds so each one stays swappable.

## Why async

Every backend uses **async** methods because it performs network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async keeps the event loop free during round-trips, which matters in microservice applications that handle many concurrent requests. Even backends with low-latency I/O (like SQLite) use async so the interface stays uniform and the loop can schedule other work during I/O waits.

## The kinds

| Kind | Examples | Role |
|---|---|---|
| **Provider** | `RedisProvider`, `PostgresProvider` | Owns the connection pool and the vendor config. |
| **Adapter** | `RedisSyncAdapter`, `RedisCacheAdapter` | Implements a `Backend` protocol over a `Provider`. |
| **Backend** | `SyncBackend`, `CacheBackend` (Protocol) | Pure interface, no implementation. |
| **Component** | `Sync`, `Cache` | Registration marker on a `Grelmicro` app: `(kind, name)` pair plus lifecycle delegation. |
| **Pattern** | `Lock`, `TaskLock`, `LeaderElection`, `TTLCache` | The user-facing primitive that resolves its adapter at use time. |

`Backend` and `Adapter` are infrastructure. Users construct **Providers**, register them on a **`Grelmicro` app** through **Components**, and import **Patterns** at module level.

## Construction vs registration

Construction and registration are two distinct steps. `__init__` validates configuration and binds locals. It performs no registry writes and no I/O. Registration happens when the adapter is attached to a `Grelmicro` app.

```python
from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.sync.redis import RedisSyncAdapter
from grelmicro.cache.redis import RedisCacheAdapter

provider = RedisProvider("redis://localhost")
micro = Grelmicro(uses=[
    provider,
    RedisSyncAdapter(provider=provider),
    RedisCacheAdapter(provider=provider),
])

async with micro:
    # every adapter is open here
    ...
# every adapter is closed on exit (LIFO)
```

First-party adapters are auto-wrapped into their canonical Component (`Sync` for sync adapters, `Cache` for cache adapters). Pass a `Sync(...)` or `Cache(...)` explicitly when you want a non-default name.

## Named backends and per-call selection

Register multiple adapters under different names and pick one at the call site:

```python
from grelmicro import Grelmicro
from grelmicro.sync import Lock, Sync
from grelmicro.sync.redis import RedisSyncAdapter
from grelmicro.sync.postgres import PostgresSyncAdapter

micro = Grelmicro(uses=[
    RedisSyncAdapter(),
    Sync(PostgresSyncAdapter(), name="analytics"),
])

Lock("cart")                       # → "default" (Redis)
Lock("audit", backend="analytics") # → "analytics" (Postgres)
Lock("cart", backend=my_adapter)   # → explicit instance, bypasses names
```

Resolution order, in priority:

1. Explicit instance (`backend=instance`).
2. The Component registered under `("sync", requested_name)`.
3. When the requested name is `"default"` and exactly one Component of that kind is registered: that sole entry.
4. Otherwise raise `ComponentNotRegisteredError`.

## Test-time overrides

`micro.override(...)` installs scoped Component swaps for the duration of a block:

```python
from grelmicro import Grelmicro
from grelmicro.sync import Lock, Sync
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.sync.redis import RedisSyncAdapter

micro = Grelmicro(uses=[RedisSyncAdapter()])
lock = Lock("cart")

async with micro:
    async with micro.override(Sync(MemorySyncAdapter())):
        async with lock:  # routed to MemorySyncAdapter
            ...
```

The override propagates downward through `await`, `asyncio.create_task`, and `asyncio.to_thread` because asyncio copies the calling context at every concurrency boundary.

## Pure construction with explicit pass-through

Skip the app entirely for one-off usage:

```python
async with RedisSyncAdapter() as backend:
    lock = Lock(name="my-lock", backend=backend)
    async with lock:
        ...
```

`async with` opens the connection only. The adapter is not registered with any app.

## Protocol-based polymorphism

Backends are defined by protocols (structural typing), not base classes. Any object implementing the required methods works. This enables:

- Swapping adapters without changing application code.
- Writing test adapters (e.g. `MemorySyncAdapter`) with no external dependencies.
- Adding new adapters without modifying existing code.

## Connection pool isolation

Each adapter instance can either share or own its connection pool depending on construction. Sharing happens through a `Provider`: pass the same `RedisProvider` to two adapters and they share one pool. Without an explicit Provider, the app dedupes implicit providers by `(provider_class, env_prefix)` so two adapters that resolve to the same vendor config still share one pool.

The default behavior is **share when possible, isolate when asked**. Pass distinct Providers to opt into per-domain isolation.

## Error handling

Accessing a Component that has not been registered raises `ComponentNotRegisteredError` with a descriptive message. Resolving a Pattern outside any `async with micro:` block raises `NoActiveAppError`.
