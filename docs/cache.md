# Cache

The `cache` package provides an in-memory TTL cache with an LRU eviction policy and a `@cached` decorator for sync and async functions.

- **[TTLCache](#ttlcache)**: In-memory cache with per-entry TTL and LRU eviction.
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
| `cache` | `TTLCache` | required | The cache instance to store results in. |
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
