# Cache

The `cache` module caches function results and arbitrary values behind a swappable backend. Use it to avoid recomputing expensive calls.

- **[TTLCache](#ttlcache)**: cache with per-entry TTL, optional maxsize with LRU eviction, and serialization.
- **[@cached](cached.md)**: decorator that caches function results automatically with stampede protection.

## Quick start

Cache an async function's result with `@cached`. One provider line says where the entries live:

```python
from grelmicro import Grelmicro
from grelmicro.cache import TTLCache, cached
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])

cache = TTLCache[User](ttl=300)


@cached(cache)
async def get_user(user_id: int) -> User:
    return await db.fetch_user(user_id)
```

Call `get_user` inside `async with micro:`, or from a handler once `micro.install(app)` ran. That is where the cache finds its backend.

Redis needs the `redis` extra: `pip install "grelmicro[redis]"`. Tests swap the provider for the memory backend, see [Testing](../testing.md).

## Backend

The cache is technology-agnostic and supports multiple backends.

You must load a cache backend before using `TTLCache`. Wire the backend
into a `Grelmicro` app via the `Cache` component. For Redis, pass the
`RedisProvider` directly to `Cache(...)`.

!!! tip "Install"
    The Redis backend needs the `redis` extra and the Postgres backend needs the `postgres` extra: `pip install "grelmicro[redis]"` or `pip install "grelmicro[postgres]"`. See the [installation guide](../installation.md) for `uv` and `poetry`.

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

=== "Memory"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.memory import MemoryProvider

    # Memory keeps entries in the process: tests and single-process apps.
    micro = Grelmicro(uses=[MemoryProvider()])
    ```

`async with micro:` opens the provider and the cache backend together.

| | Redis | Postgres | SQLite | Memory |
|---|---|---|---|---|
| **Use case** | Production | Production (when Postgres is already deployed) | Single-host with restart durability | Testing / single-process |
| **Multi-node** | Yes | Yes | No (single file) | No |
| **Persistence** | Yes (auto-expiring keys) | Yes (table-backed) | Yes (file-backed) | No |

The Postgres adapter stores entries in a single `grelmicro_cache` table keyed on `key TEXT PRIMARY KEY` with `value BYTEA` and `expires_at TIMESTAMPTZ`. `get` filters expired rows with `WHERE expires_at > NOW()`, `set` is one `INSERT ... ON CONFLICT DO UPDATE`, `delete` and `clear` are single statements. The table is created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema. Set `cleanup_interval=` to enable a background janitor that reclaims rows expired for more than one hour.

On a Redis Cluster, give the adapter's `prefix` a hash tag so its multi-key operations stay in one slot. See [the hash-tag rule](../providers/redis.md#the-hash-tag-rule-on-cluster). Use `prefix` on any backend to isolate cache keys from other data in the same server.

### Choosing a backend

Pick the backend that matches your deployment, not the fastest one on paper.

- **Memory**: use for tests and single-process apps. Entries live in the process and disappear on restart. Each node keeps its own copy, so it does not share a cache across nodes.
- **Redis**: use for a distributed cache shared by many nodes. Keys auto-expire and reads stay fast, so this is the default for production. Reach for it when you already run or can add Redis.
- **PostgreSQL**: use when Postgres is already in your stack or you want table-backed persistence. It needs no extra infrastructure and survives restarts. Slightly slower than Redis, but the right default when you want one fewer moving part.
- **SQLite**: use for a single-host app that wants a cache surviving restarts with no extra service. Entries live in one file, so it does not share a cache across hosts.

## TTLCache

`TTLCache` is the main cache class. It delegates storage to the backend the app registered and handles TTL, optional maxsize with LRU eviction, serialization, and statistics.

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

`Cache.ttl(...)` builds one from the component itself, so the cache and its backend are declared in the same place:

```python title="redis_basic.py"
--8<-- "cache/redis_basic.py"
```

### Serialization

Backends store raw bytes. To cache Python objects, name the type:

=== "Pydantic Model (recommended)"

    Type-safe roundtrips using Pydantic's Rust-based TypeAdapter (fastest option):

    ```python
    from pydantic import BaseModel

    from grelmicro.cache import TTLCache

    class User(BaseModel):
        id: int
        name: str

    cache = TTLCache[User](ttl=300)

    await cache.set("user", User(id=1, name="Alice"))
    user = await cache.get("user")  # returns User instance
    ```

    The type parameter picks the serializer. Anything Pydantic can adapt
    works, including a dataclass, a `TypedDict`, and `list[User]`.

    Where there is no type parameter to read, such as the `Cache.ttl`
    factory, pass the type itself:

    ```python
    cache = micro.cache.ttl(ttl=300, serializer=User)
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

With no type parameter and no serializer, only `bytes` values are accepted. `TTLCache[bytes]` also stores raw bytes.

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

The factory shares the same stampede protection as [`@cached(lock=True)`](cached.md#stampede-protection). When many callers miss the same key at once, the factory runs once and the rest reuse its result. This works across replicas when a `Coordination` backend is configured.

Pass `stale_ttl=` to serve the last good value when the factory fails, the same serve-stale-on-error behavior as [`@cached(stale_ttl=...)`](cached.md#serve-stale-on-error).

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

Literal tags with no `{...}` pass through unchanged. Tags work the same across Memory, Redis, and Postgres. Invalidating by tag stays consistent even when keys expire on their own. The [`@cached` decorator takes tags too](cached.md#tags), filled in from the call's arguments.

```python title="tags.py"
--8<-- "cache/tags.py"
```

!!! warning "Keep keys and tags bounded"
    Every distinct key and tag is stored. On the memory backend the tag-to-key map grows with cardinality and is not evicted until the tagged entries expire. Deriving keys or tags straight from untrusted input (a raw user id, a full URL, a free-text field) lets a caller inflate memory or backend storage without limit. Map untrusted values onto a bounded set first, such as a hash bucket or an allowlist, and prefer a short shared tag plus one per-entity tag over a fresh tag per request.
