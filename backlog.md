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

### 5. LRU Eviction Policy

Replaced FIFO eviction with LRU (Least Recently Used). Accessing a key via `get()` promotes it to most-recently-used. Eviction removes the LRU entry first (after expired entries).

### 6. Stampede Protection

Added optional `lock` parameter to `@cached`. Uses double-checked locking: first check without lock, then re-check after acquiring lock. Supports `asyncio.Lock()` for async and `threading.Lock()` for sync functions.
