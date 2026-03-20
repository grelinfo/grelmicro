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

Use the `lock` parameter to prevent multiple concurrent callers from recomputing the same cache entry. Only one caller executes the function while others wait for the result:

```python
--8<-- "cache/lock.py"
```

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
| `lock` | context manager | `None` | Lock for stampede protection (`asyncio.Lock()` or `threading.Lock()`). |

!!! warning
    **Thread Safety:** `TTLCache` is not thread-safe. The caller is responsible for synchronization when accessing the cache from multiple threads.
