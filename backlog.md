# Cache Module Backlog

## Completed

### 1. Cache Statistics

Added `hits`, `misses`, `evictions`, and `currsize` tracking to `TTLCache` via `cache_info()` returning a frozen `CacheInfo` dataclass.

### 2. Decorator Cache Control Methods

Decorated functions now expose `cache_info()` and `cache_clear()` matching `functools.lru_cache` convention.

### 3. Skip Condition

Added `skip` parameter to `@cached` to conditionally skip caching based on the result.

### 4. Typed Key Generation

Added `typed` parameter to `@cached` and `make_cache_key` to distinguish argument types in cache keys.

## Medium Value, Medium Effort

### 5. LRU Eviction Policy

Current eviction is FIFO. LRU (Least Recently Used) is the industry default (`functools`, `cachetools`). Options:

- Replace FIFO with LRU as the default.
- Offer both via a policy parameter.

LRU requires updating entry order on every `get()` hit, adding a small overhead.

### 6. Stampede Protection

Add optional `lock` parameter to `@cached` so only one caller recomputes on cache miss while others wait for the result.

```python
lock = asyncio.Lock()

@cached(cache, lock=lock)
async def fetch_expensive(key: str) -> dict: ...
```

Critical for expensive async calls under high concurrency. Pattern from `cachetools` (uses `threading.Condition`).
