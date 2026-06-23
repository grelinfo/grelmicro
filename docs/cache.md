# Cache

The `cache` module caches function results and arbitrary values behind a swappable backend. Use it to avoid recomputing expensive calls.

- **[TTLCache](#ttlcache)**: cache with per-entry TTL, optional maxsize with LRU eviction, and serialization.
- **[@cached](#cached-decorator)**: decorator that caches function results automatically with stampede protection.

## Quick start

Cache an async function's result with `@cached`. The Memory backend needs no extra service, so this runs as-is. Swap in Redis or Postgres for production:

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache, cached
from grelmicro.cache.memory import MemoryCacheAdapter

micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

cache = TTLCache(ttl=300, serializer=JsonSerializer())


@cached(cache)
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

## Backend

The cache is technology-agnostic and supports multiple backends.

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
    micro = Grelmicro(uses=[Cache(redis)])
    ```

=== "Postgres"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    micro = Grelmicro(uses=[Cache(postgres)])
    ```

=== "SQLite"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.providers.sqlite import SQLiteProvider

    sqlite = SQLiteProvider("app.db")
    micro = Grelmicro(uses=[Cache(sqlite)])
    ```

`async with micro:` opens the provider and the cache backend together.

| | Redis | Postgres | SQLite | Memory |
|---|---|---|---|---|
| **Use case** | Production | Production (when Postgres is already deployed) | Single-host with restart durability | Testing / single-process |
| **Multi-node** | Yes | Yes | No (single file) | No |
| **Persistence** | Yes (auto-expiring keys) | Yes (table-backed) | Yes (file-backed) | No |

The Postgres adapter stores entries in a single `grelmicro_cache` table keyed on `key TEXT PRIMARY KEY` with `value BYTEA` and `expires_at TIMESTAMPTZ`. `get` filters expired rows with `WHERE expires_at > NOW()`, `set` is one `INSERT ... ON CONFLICT DO UPDATE`, `delete` and `clear` are single statements. The table is created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema. Set `cleanup_interval=` to enable a background janitor that reclaims rows expired for more than one hour.

### Choosing a backend

Pick the backend that matches your deployment, not the fastest one on paper.

- **Memory**: use for tests and single-process apps. Entries live in the process and disappear on restart. Each node keeps its own copy, so it does not share a cache across nodes.
- **Redis**: use for a distributed cache shared by many nodes. Keys auto-expire and reads stay fast, so this is the default for production. Reach for it when you already run or can add Redis.
- **PostgreSQL**: use when Postgres is already in your stack or you want table-backed persistence. It needs no extra infrastructure and survives restarts. Slightly slower than Redis, but the right default when you want one fewer moving part.
- **SQLite**: use for a single-host app that wants a cache surviving restarts with no extra service. Entries live in one file, so it does not share a cache across hosts.

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

### Get or Set

`get_or_set` returns the cached value, or computes it once and stores it. Pass a sync or async factory. It runs only on a miss:

```python
user = await cache.get_or_set(
    "user:1",
    lambda: fetch_user(1),
    tags=["users"],
)
```

The factory shares the same stampede protection as `@cached(lock=True)`. When many callers miss the same key at once, the factory runs once and the rest reuse its result. This works across replicas when a `Coordination` backend is configured.

Pass `stale_ttl=` to serve the last good value when the factory fails, the same serve-stale-on-error behavior as [`@cached(stale_ttl=...)`](#serve-stale-on-error).

```python title="get_or_set.py"
--8<-- "cache/get_or_set.py"
```

### Batch Operations

Read, write, and delete many keys in one call:

```python
await cache.set_many({"user:1": user1, "user:2": user2}, tags=["users"])

found = await cache.get_many(["user:1", "user:2", "user:3"])
# Missing keys are absent from the result.

await cache.delete_many(["user:1", "user:2"])
```

```python title="batch.py"
--8<-- "cache/batch.py"
```

### Tags and Invalidation

Tags group entries so you can drop a whole group at once. Tag an entry on `set`, `set_many`, or `get_or_set`, then invalidate by tag with `delete_tags`:

```python
await cache.set("user:1", user, tags=["users", "user:1"])

await cache.delete_tags("user:1")   # drop one user
await cache.delete_tags("users")    # drop every user
```

The `@cached` decorator takes tags too. Each tag is a template filled in from the call's arguments, so one decorator tags every entry with both a shared tag and a per-call tag:

```python
@cached(cache, tags=["users", "user:{user_id}"])
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)


# Later, after a write:
await cache.delete_tags("user:42")   # drop the entry for user_id=42
await cache.delete_tags("users")     # drop every cached user
```

Literal tags with no `{...}` pass through unchanged. Tags work the same across Memory, Redis, and Postgres. Invalidating by tag stays consistent even when keys expire on their own.

```python title="tags.py"
--8<-- "cache/tags.py"
```

## @cached Decorator

The `@cached` decorator automatically caches function results. It works with both sync and async functions.

For the plain "memoize this function for N seconds" case, pass `ttl=` and nothing else. The decorator builds a private process-local cache for this function alone:

```python
from grelmicro.cache import cached

@cached(ttl=30)
async def get_rates() -> dict:
    return await fetch_rates()
```

That private cache lives only in this process and is never shared across replicas. To share results across replicas, invalidate by tag, or reuse one store across functions, pass a `TTLCache` instead:

```python
from grelmicro.cache import JsonSerializer, TTLCache, cached

cache = TTLCache(ttl=300, serializer=JsonSerializer())

@cached(cache)
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

Passing both `cache` and `ttl`, or neither, raises `TypeError`.

### Custom Keys

By default `@cached` derives the key from the `repr()` of the arguments. Pass `key=` for a stable, readable key instead. The template fills in from the call's arguments, so `key="user:{user_id}"` keys the entry under `user:42` for a call with `user_id=42`:

```python
@cached(cache, key="user:{user_id}")
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

Arguments not named in the template do not affect the key, so calls that differ only in those arguments share one entry. Defaults fill in when an argument is omitted. For a fully dynamic key, pass a `key_maker` callable instead. It receives `(func, args, kwargs)` and returns the key. Passing both `key` and `key_maker` raises `TypeError`. A custom key fully determines the lookup, so `typed=` has no effect when `key=` or `key_maker` is set.

```python title="key.py"
--8<-- "cache/key.py"
```

### Stampede Protection

A cache stampede (or "dog-pile") happens when many callers miss the same key at once and all recompute it together. By default `@cached` folds those misses in-process (`lock="local"`). Raise it to `lock=True` to fold across replicas, drop it to `lock=False` to opt out, and add `early=` to refresh hot keys before they expire:

| Setting | What it does | Cost | Use when |
|---|---|---|---|
| `lock="local"` (default) | fold misses in-process only, never touches a backend | free, no I/O | the common case |
| `lock=True` | fold concurrent misses, across replicas when a `Coordination` backend is configured | one backend acquire per cold miss | you need cross-replica dedup |
| `lock=False` | no protection, every concurrent miss recomputes | none | misses are cheap or rare |
| `early=0.1` | probabilistic early refresh (XFetch) in the last 10% of the TTL | one background recompute per refresh | the hottest keys, where no caller should ever block |

`lock=True` always dedups in-process first, so the backend is hit at most once per cold miss. `early=` works alongside any lock mode.

```python
@cached(cache)                  # default: in-process stampede folding
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)


@cached(cache, lock=True)       # fold misses, across replicas if a lock backend is set
async def get_billing(user_id: int) -> dict:
    return await billing.fetch(user_id)


@cached(cache, early=0.1)       # refresh hot keys before they expire
async def get_homepage_feed() -> dict:
    return await build_feed()
```

`lock` is **per-key**: concurrent misses on different keys run in parallel. Only callers that request the same key wait in turn, so one slow computation does not block unrelated keys.

`lock=True` folds misses across replicas when the active `Grelmicro` app has a `Coordination` backend, and folds them in-process when it does not. Use `lock="local"` to force the in-process path even when a `Coordination` backend is configured.

`early=` returns the cached value immediately and recomputes in the background, so a hot key refreshes before it expires and no caller ever waits on a cold miss. It costs one extra recompute per refresh and stores a small sidecar entry next to the value so replicas coordinate the refresh window.

**When to use:** your cached function is expensive (database query, API call, heavy computation) and may be called concurrently with the same arguments.

### Serve Stale on Error

Set `stale_ttl` to keep serving the last good value when a recompute fails. Each result is also kept as a fallback copy for `ttl + stale_ttl` seconds. After the TTL, the next miss recomputes as usual, but if that recompute raises, the most recent value is served instead of propagating the error, for up to `stale_ttl` seconds past the TTL.

```python
cache = TTLCache(ttl=60)

@cached(cache, stale_ttl=600)
async def get_exchange_rates() -> dict:
    return await rates_api.fetch()   # a flaky external call
```

A flaky upstream then degrades to slightly stale data instead of an error storm. Once the recompute succeeds again, the fresh value takes over. If the upstream stays down longer than `stale_ttl`, the error propagates.

`stale_ttl` composes with `lock` and `early`. An explicit `cache.delete(...)` or `cache.delete_tags(...)` drops the fallback too, so invalidation is never undone by a later stale serve. Each stale serve records the `grelmicro.cache.stale_serves` metric, so a rising count signals an unhealthy upstream.

### Decorator Parameters

`cache` and `ttl` are mutually exclusive. Pass one or the other, not both.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cache` | `TTLCache` | `None` | The cache instance to store results in. Mutually exclusive with `ttl`. |
| `ttl` | `float` | `None` | TTL in seconds for a private per-function cache. Mutually exclusive with `cache`. |
| `maxsize` | `int` | `0` | Max entries in the private per-function cache, `0` means unlimited (used only when `ttl` is set). |
| `key` | `str` | `None` | Key template rendered from the arguments, like `"user:{user_id}"`. Mutually exclusive with `key_maker`. |
| `key_maker` | `Callable` | `None` | Custom key generation function. Receives `(func, args, kwargs)`. Mutually exclusive with `key`. |
| `skip` | `Callable` | `None` | Predicate receiving the result. Returns `True` to skip caching. |
| `typed` | `bool` | `False` | Cache arguments of different types separately. |
| `lock` | `True`, `False`, or `"local"` | `False` | Concurrent-miss (stampede) protection. |
| `early` | `float` in `[0, 1)` | `None` | Probabilistic early refresh in the late TTL window. |
| `stale_ttl` | `float` | `None` | Serve-stale-on-error budget in seconds. Serve the last good value for this long past the TTL when a recompute fails. |
| `tags` | `Sequence[str]` | `()` | Tags to attach to each result. Templates like `"user:{user_id}"` fill in from the arguments. Invalidate with `cache.delete_tags(...)`. |

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
