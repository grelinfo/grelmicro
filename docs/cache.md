# Cache

The `cache` module provides caching with swappable backends and a `@cached` decorator for caching function results.

The cache is technology-agnostic, supporting multiple backends (see more in the Backend section).

- **[TTLCache](#ttlcache)**: Cache with per-entry TTL, optional maxsize with LRU eviction, and serialization.
- **[@cached](#cached-decorator)**: Decorator that caches function results automatically with stampede protection.

## Backend

You must load a cache backend before using `TTLCache`. Wire the backend
into a `Grelmicro` app via the `Cache` component. For Redis, pass the
`RedisProvider` directly to `Cache(...)`.

!!! tip "Install"
    The Redis backend needs the `redis` extra and the Postgres backend needs the `postgres` extra: `pip install "grelmicro[redis]"` or `pip install "grelmicro[postgres]"`. See the [installation guide](installation.md) for `uv` and `poetry`.

=== "Memory"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.cache.memory import MemoryCacheAdapter

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    ```

=== "Redis"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.providers.redis import RedisProvider

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[redis, Cache(redis)])
    ```

=== "Postgres"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    micro = Grelmicro(uses=[postgres, Cache(postgres)])
    ```

`async with micro:` opens the provider and the cache backend together.

| | Redis | Postgres | Memory |
|---|---|---|---|
| **Use case** | Production | Production (when Postgres is already deployed) | Testing / single-process |
| **Multi-node** | Yes | Yes | No |
| **Persistence** | Yes (auto-expiring keys) | Yes (table-backed) | No |

The Postgres adapter stores entries in a single `grelmicro_cache` table keyed on `key TEXT PRIMARY KEY` with `value BYTEA` and `expires_at TIMESTAMPTZ`. `get` filters expired rows with `WHERE expires_at > NOW()`, `set` is one `INSERT ... ON CONFLICT DO UPDATE`, `delete` and `clear` are single statements. The table is created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema. Set `cleanup_interval=` to enable a background janitor that reclaims rows expired for more than one hour.

### Choosing a backend

Pick the backend that matches your deployment, not the fastest one on paper.

- **Memory**: use for tests and single-process apps. Entries live in the process and disappear on restart. Each node keeps its own copy, so it does not share a cache across nodes.
- **Redis**: use for a distributed cache shared by many nodes. Keys auto-expire and reads stay fast, so this is the default for production. Reach for it when you already run or can add Redis.
- **PostgreSQL**: use when Postgres is already in your stack or you want table-backed persistence. It needs no extra infrastructure and survives restarts. Slightly slower than Redis, but the right default when you want one fewer moving part.

## TTLCache

`TTLCache` is the main cache class. It delegates storage to the registered backend and handles TTL, optional maxsize with LRU eviction, serialization, and statistics.

```python
from grelmicro.cache import TTLCache

# Uses the registered backend (MemoryCacheAdapter or RedisCacheAdapter)
cache = TTLCache(maxsize=100, ttl=300)

# Or pass a backend explicitly
cache = TTLCache(maxsize=100, ttl=300, backend=my_backend)
```

All `TTLCache` methods are async:

```python
await cache.set("key", b"value")
result = await cache.get("key")
await cache.delete("key")
await cache.clear()
```

### Serialization

Backends store raw bytes. To cache Python objects, pass a serializer:

=== "Pydantic Model (recommended)"

    Type-safe roundtrips using Pydantic's Rust-based TypeAdapter (fastest option):

    ```python
    from pydantic import BaseModel

    from grelmicro.cache import PydanticSerializer, TTLCache

    class User(BaseModel):
        id: int
        name: str

    cache = TTLCache[User](ttl=300, serializer=PydanticSerializer(User))

    await cache.set("user", User(id=1, name="Alice"))
    user = await cache.get("user")  # returns User instance
    ```

=== "JSON"

    For plain dicts and lists, using orjson when available:

    ```python
    from grelmicro.cache import JsonSerializer, TTLCache

    cache = TTLCache(ttl=300, serializer=JsonSerializer())

    await cache.set("user", {"id": 1, "name": "Alice"})
    user = await cache.get("user")  # returns dict
    ```

=== "Pickle (trusted backends only)"

    For any picklable Python object. **Use only with trusted, in-process
    backends.** Deserialization can execute arbitrary code, so a shared
    or compromised backend can run code inside the application. Prefer
    `JsonSerializer` or `PydanticSerializer` for shared backends like
    Redis or Memcached.

    ```python
    from grelmicro.cache import PickleSerializer, TTLCache

    cache = TTLCache(ttl=300, serializer=PickleSerializer())

    await cache.set("data", {"complex": [1, 2, 3]})
    data = await cache.get("data")
    ```

Without a serializer, only `bytes` values are accepted.

### Per-Entry TTL

Override the default TTL for individual entries:

```python
await cache.set("session", b"token", ttl=3600)  # 1 hour instead of default
```

## @cached Decorator

The `@cached` decorator automatically caches function results. It works with both sync and async functions.

```python
from grelmicro.cache import JsonSerializer, TTLCache, cached

cache = TTLCache(ttl=300, serializer=JsonSerializer())

@cached(cache)
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

### Stampede Protection

A cache stampede (or "dog-pile") happens when many callers miss the same key at once and all recompute it together. `@cached` ships a three-layer menu, opt-in by cost:

| Layer | What it does | Cost | Use when |
|---|---|---|---|
| `stampede="local"` (default) | per-key in-process lock | free, no I/O | always, the cheap correct default |
| `stampede="distributed"` | cross-replica lock via the `Sync` component | one backend acquire per cold miss | a hot key on many replicas |
| `early=0.1` | probabilistic early refresh (XFetch) in the last 10% of the TTL | one background recompute per refresh | the hottest keys, where no caller should ever block |

The layers compose. `stampede="distributed"` implies `"local"` (in-process dedup is free), and `early=` works with either.

```python
@cached(cache)                          # default: stampede="local"
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)


@cached(cache, stampede="distributed")  # cross-replica via the Sync component
async def get_billing(user_id: int) -> dict:
    return await billing.fetch(user_id)


@cached(cache, early=0.1)               # refresh hot keys before they expire
async def get_homepage_feed() -> dict:
    return await build_feed()
```

`stampede="local"` is **per-key**: concurrent misses on different keys run in parallel. Only callers that request the same key wait in turn, so one slow computation does not block unrelated keys.

`stampede="distributed"` resolves the `Sync` component from the active `Grelmicro` app, so it needs an app with a `Sync` backend. Set `stampede=None` to opt out entirely.

`early=` returns the cached value immediately and recomputes in the background, so a hot key refreshes before it expires and no caller ever waits on a cold miss. It costs one extra recompute per refresh and stores a small sidecar entry next to the value so replicas coordinate the refresh window.

**When to use:** your cached function is expensive (database query, API call, heavy computation) and may be called concurrently with the same arguments.

### Decorator Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cache` | `TTLCache` | required | The cache instance to store results in. |
| `key_maker` | `Callable` | `None` | Custom key generation function. Receives `(func, args, kwargs)`. |
| `skip` | `Callable` | `None` | Predicate receiving the result. Returns `True` to skip caching. |
| `typed` | `bool` | `False` | Cache arguments of different types separately. |
| `stampede` | `"local"`, `"distributed"`, or `None` | `"local"` | Concurrent-miss protection. |
| `early` | `float` in `[0, 1)` | `None` | Probabilistic early refresh in the late TTL window. |

## Redis Backend Configuration

The Redis URL can be passed directly or read from environment variables:

| Environment Variable | Description | Default |
|---|---|---|
| `REDIS_URL` | Full Redis URL (e.g. `redis://localhost:6379/0`) | |
| `REDIS_HOST` | Redis hostname | |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_DB` | Redis database number | `0` |
| `REDIS_PASSWORD` | Redis password | |

Set either `REDIS_URL` or `REDIS_HOST` (not both).

Use the `prefix` parameter to isolate cache keys from other data in the same Redis instance.

!!! warning
    **Cache Key Stability:** Cache keys are derived from `repr()` of function arguments. Keys are stable within a single process but may vary across Python versions. Objects with default `__repr__` (e.g., custom class instances) include memory addresses, which means cache misses will always occur. Use a custom `key_maker` for such objects.
