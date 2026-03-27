# Cache

The `cache` package provides cache backends and a `@cached` decorator for sync and async functions. Backends are swappable: use the in-memory `TTLCache` for single-process applications, or `RedisCache` for distributed caching.

- **[TTLCache](#ttlcache)**: In-memory cache with per-entry TTL and LRU eviction.
- **[RedisCache](#redis-cache)**: Distributed cache backed by Redis.
- **[@cached](#cached-decorator)**: Decorator that caches function results automatically.

## TTLCache

`TTLCache` is a synchronous in-memory cache where each entry expires after a configurable time-to-live (TTL). When the cache is full, expired entries are evicted first, then the least recently used (LRU) entry.

### Usage

```python
--8<-- "cache/basic.py"
```

## @cached Decorator

The `@cached` decorator automatically caches function results. It detects whether the decorated function is sync or async and wraps it accordingly.

### Skip Condition

Use the `skip` parameter to avoid caching unwanted results (e.g., `None` or error responses):

```python
--8<-- "cache/skip.py"
```

### Stampede Protection

Set `lock=True` to prevent multiple concurrent callers from recomputing the same cache entry on a cache miss. When enabled, only one caller executes the function while all others **block and wait** until the result is available:

```python
--8<-- "cache/lock.py"
```

The decorator auto-creates the appropriate lock type based on the decorated function (`asyncio.Lock()` for async, `threading.Lock()` for sync).

Locking is **per-key**: concurrent misses on different keys proceed in parallel. Only callers hitting the same key are serialized, so one slow computation does not block unrelated keys.

**When to use:** your cached function is expensive (database query, API call, heavy computation) and may be called concurrently with the same arguments. Without a lock, all callers that miss the cache would execute the function redundantly.

!!! note
    You can also pass a custom context manager instance (e.g. `lock=my_lock`) for global locking where a single lock is shared across all keys.

### Statistics and Control

Decorated functions expose `cache_info()` and `cache_clear()` methods matching the `functools.lru_cache` interface:

```python
--8<-- "cache/stats.py"
```

### Decorator Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cache` | `Cache` or `CacheBackend` | required | The cache instance to store results in (e.g. `TTLCache` or `RedisCache`). |
| `key_maker` | `Callable` | `None` | Custom key generation function. Receives `(func, args, kwargs)`. |
| `serializer` | `Callable` | `None` | Serializer for cached values. Must be paired with `deserializer`. |
| `deserializer` | `Callable` | `None` | Deserializer for cached values. Must be paired with `serializer`. |
| `skip` | `Callable` | `None` | Predicate receiving the result. Returns `True` to skip caching. |
| `typed` | `bool` | `False` | Cache arguments of different types separately. |
| `lock` | `bool` or context manager | `None` | Stampede protection. `True` enables per-key locking. See [Stampede Protection](#stampede-protection). |

!!! warning
    **Thread Safety:** `TTLCache` is not thread-safe. The caller is responsible for synchronization when accessing the cache from multiple threads.

!!! warning
    **Cache Key Stability:** Cache keys are derived from `repr()` of function arguments. Keys are stable within a single process but may vary across Python versions. Objects with default `__repr__` (e.g., custom class instances) include memory addresses, which means cache misses will always occur. Use a custom `key_maker` for such objects.

## Redis Cache

`RedisCache` is an async cache backend that stores entries in Redis with a configurable TTL. Use it for distributed caching across multiple processes or services.

### Installation

```bash
pip install grelmicro[redis]
```

### Usage

```python
--8<-- "cache/redis_basic.py"
```

`RedisCache` must be used as an async context manager. The connection is closed when the context exits.

### Configuration

The Redis URL can be passed directly or read from environment variables:

| Environment Variable | Description | Default |
|---|---|---|
| `REDIS_URL` | Full Redis URL (e.g. `redis://localhost:6379/0`) | |
| `REDIS_HOST` | Redis hostname | |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_DB` | Redis database number | `0` |
| `REDIS_PASSWORD` | Redis password | |

Set either `REDIS_URL` or `REDIS_HOST` (not both).

### Key Prefix

Use the `prefix` parameter to isolate cache keys from other data in the same Redis instance:

```python
cache = RedisCache(prefix="myapp:", ttl=300)
```

All keys are stored as `{prefix}{key}`. The `clear()` method only removes keys matching the prefix.

### Serialization

Redis stores bytes. When using `RedisCache` with `@cached`, you must provide `serializer` and `deserializer` to convert values to and from bytes.

### Differences from TTLCache

| Feature | TTLCache | RedisCache |
|---|---|---|
| Storage | In-memory (process-local) | Redis (distributed) |
| Function type | Sync and async | Async only |
| `maxsize` | Configurable | Managed by Redis eviction policy |
| Serialization | Optional | Required (Redis stores bytes) |
| `cache_info().currsize` | Exact count | Always 0 (counting prefixed keys is expensive) |
| `cache_clear()` | Sync | Async (must be awaited) |
