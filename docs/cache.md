# Cache

The `cache` module provides caching with swappable backends and a `@cached` decorator for caching function results.

The cache is technology-agnostic, supporting multiple backends (see more in the Backend section).

- **[TTLCache](#ttlcache)**: Cache with per-entry TTL, optional maxsize with LRU eviction, and serialization.
- **[@cached](#cached-decorator)**: Decorator that caches function results automatically with stampede protection.

## Backend

You must load a cache backend before using `TTLCache`.

!!! tip "Install"
    The Redis backend needs the `redis` extra: `pip install "grelmicro[redis]"`. See the [installation guide](installation.md) for `uv` and `poetry`.

=== "Memory"
    ```python
    from grelmicro.cache.memory import MemoryCacheAdapter

    backend = MemoryCacheAdapter()
    ```

=== "Redis"
    ```python
    from grelmicro.cache.redis import RedisCacheAdapter

    backend = RedisCacheAdapter("redis://localhost:6379/0", prefix="myapp:")
    ```

Backends must be used as async context managers:

```python
async with backend:
    # cache operations here
    ...
```

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

=== "Pickle"

    For any picklable Python object:

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
from grelmicro.cache import TTLCache, cached

cache = TTLCache(ttl=300, serializer=PickleSerializer())

@cached(cache)
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

### Stampede Protection

Set `lock=True` to prevent multiple concurrent callers from recomputing the same cache entry on a cache miss. When enabled, only one caller executes the function while all others **block and wait** until the result is available:

```python
@cached(cache, lock=True)
async def get_user(user_id: int) -> dict:
    return await db.fetch_user(user_id)
```

Locking is **per-key**: concurrent misses on different keys run in parallel. Only callers that request the same key wait in turn, so one slow computation does not block unrelated keys.

**When to use:** your cached function is expensive (database query, API call, heavy computation) and may be called concurrently with the same arguments.

### Decorator Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cache` | `TTLCache` | required | The cache instance to store results in. |
| `key_maker` | `Callable` | `None` | Custom key generation function. Receives `(func, args, kwargs)`. |
| `skip` | `Callable` | `None` | Predicate receiving the result. Returns `True` to skip caching. |
| `typed` | `bool` | `False` | Cache arguments of different types separately. |
| `lock` | `bool` or context manager | `None` | Stampede protection. `True` enables per-key locking. |

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
