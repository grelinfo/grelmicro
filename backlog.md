# Cache Module Backlog

## High Value, Low Effort

### 1. Cache Statistics

Add `hits`, `misses`, and `currsize` tracking to `TTLCache`.

Standard pattern from `functools.lru_cache`, `cachetools`, and `aiocache`. Essential for production monitoring and tuning cache sizes.

### 2. Decorator Cache Control Methods

Expose `cache_info()` and `cache_clear()` on decorated functions.

```python
@cached(cache)
async def fetch(user_id: int) -> dict: ...

fetch.cache_info()   # CacheInfo(hits=42, misses=7, maxsize=100, currsize=49)
fetch.cache_clear()  # Clear all entries
```

Matches `functools.lru_cache` convention that users already expect.

### 3. Skip Condition

Add `skip` parameter to `@cached` to conditionally skip caching based on the result.

```python
@cached(cache, skip=lambda r: r is None)
async def fetch(user_id: int) -> dict | None: ...
```

Prevents polluting the cache with error/empty responses. Pattern from `aiocache` and `cashews`.

### 4. Typed Key Generation

Add `typed` parameter to `@cached` to distinguish argument types in cache keys.

```python
@cached(cache, typed=True)
def compute(x: int | float) -> str: ...

compute(3)    # cached separately
compute(3.0)  # cached separately
```

Without this, `3` and `3.0` produce the same `repr()` and share a cache entry. Pattern from `functools.lru_cache`.

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
