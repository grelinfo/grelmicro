# The @cached Decorator

The `@cached` decorator automatically caches function results. It works with both sync and async functions.

For the plain "memoize this function for N seconds" case, pass `ttl=` and nothing else. The decorator builds a private process-local cache for this function alone:

```python
from grelmicro.cache import cached

@cached(ttl=30)
async def get_rates() -> Rates:
    return await fetch_rates()
```

That private cache lives only in this process and is never shared across replicas. To share results across replicas, invalidate by tag, or reuse one store across functions, pass a [`TTLCache`](index.md#ttlcache) instead:

```python
from grelmicro.cache import TTLCache, cached

cache = TTLCache[User](ttl=300)

@cached(cache)
async def get_user(user_id: int) -> User:
    return await db.fetch_user(user_id)
```

Passing both `cache` and `ttl`, or neither, raises `TypeError`.

## Custom Keys

By default `@cached` derives the key from the `repr()` of the arguments. Pass `key=` for a stable, readable key instead. The template fills in from the call's arguments, so `key="user:{user_id}"` keys the entry under `user:42` for a call with `user_id=42`:

```python
@cached(cache, key="user:{user_id}")
async def get_user(user_id: int) -> User:
    return await db.fetch_user(user_id)
```

Arguments not named in the template do not affect the key, so calls that differ only in those arguments share one entry. Defaults fill in when an argument is omitted. For a fully dynamic key, pass a `key_maker` callable instead. It receives `(func, args, kwargs)` and returns the key. Passing both `key` and `key_maker` raises `TypeError`. A custom key fully determines the lookup, so `typed=` has no effect when `key=` or `key_maker` is set.

```python title="key.py"
--8<-- "cache/key.py"
```

!!! warning "Default keys are not stable across processes"
    The default key is the `repr()` of the arguments. Keys are stable within a single process but may vary across Python versions. An object using the default `__repr__` carries a memory address, so its key changes on every restart and the entry is never found again. Pass `key=` or `key_maker=` for such objects.

## Tags

Each tag is a template filled in from the call's arguments, so one decorator tags every entry with both a shared tag and a per-call tag:

```python
@cached(cache, tags=["users", "user:{user_id}"])
async def get_user(user_id: int) -> User:
    return await db.fetch_user(user_id)


# Later, after a write:
await cache.delete_tags("user:42")   # drop the entry for user_id=42
await cache.delete_tags("users")     # drop every cached user
```

See [Tags and Invalidation](index.md#tags-and-invalidation) for how tags behave on each backend.

## On a method

A method must name its key. Decorating one without `key=` or `key_maker=` raises `TypeError` at decoration time:

```python
class Repo:
    @cached(cache, key="repo:{user_id}")
    async def load(self, user_id: int) -> User:
        return await self.db.fetch_user(user_id)
```

The default key is the `repr()` of every argument, and on a method the first one is `self`. That reads two ways, both wrong. Two instances whose `repr()` matches share one entry, so a call on one returns the other's value. An instance using the default `repr()` carries a memory address, so its key changes on every restart and the entry is never found again.

Only you know what identifies the entry, so the decorator asks rather than guesses. Name the fields that matter and leave `self` out. When the result does depend on instance state, fold that state into the key:

```python
class Repo:
    @cached(cache, key="repo:{self.region}:{user_id}")
    async def load(self, user_id: int) -> User: ...
```

A `staticmethod` and a `classmethod` are untouched. Neither receives an instance, and a class `repr()` is stable, so their default key is sound.

`refresh()` on a method is reached through the class, because attribute access on an instance returns the helper unbound:

```python
await Repo.load.refresh(repo, 42)
```

## On-demand refresh

`refresh()` recomputes for the given arguments, overwrites the stored entry, and returns the new value. It skips the read, so a live entry is replaced rather than served.

```python title="refresh.py"
--8<-- "cache/refresh.py"
```

Use it where a caller asks for fresh data, such as an endpoint honouring `Cache-Control: no-cache`:

```python
@app.post("/reports/{user_id}")
async def report(user_id: int, cache_control: Annotated[str, Header()] = "") -> Report:
    if "no-cache" in cache_control:
        return await get_report.refresh(user_id)
    return await get_report(user_id)
```

Two things differ from a normal miss:

- **Refreshes do not fold.** A miss folds concurrent callers into one execution, but every refresh runs the function itself. A refresh never returns a value computed before the call started, so writing to the database and then refreshing cannot hand you pre-write data. Under the default `lock`, refreshes for one key also serialize within the process, and with `lock=True` and a lock backend they serialize across replicas. Under `lock=False` they run in parallel and the last write wins.
- **Errors propagate**, even under `stale_ttl`. Serving the stale value would return the exact entry the caller asked to bypass. Compose it yourself when you want that:

```python
try:
    return await get_report.refresh(user_id)
except UpstreamError:
    return await get_report(user_id)
```

A refresh honours `skip`, `tags`, and the `stale_ttl` reserve, so the stored entry stays consistent with what a normal miss would have written. A result the `skip` predicate rejects is not stored, so the previous entry stays and keeps being served. On a sync function `refresh()` returns the value directly, matching the call it mirrors.

Refreshing a key that was never cached simply computes and stores it. A refresh reads nothing, so it counts as neither a hit nor a miss in `cache_info()`. It works on the private `@cached(ttl=...)` form too.

!!! tip "Refreshing one key, not the whole cache"
    `cache_clear()` is bound to the whole `TTLCache`, so on a cache shared between functions it removes every function's entries. `refresh()` touches one key.

## Streaming producers

Decorate an async generator and `@cached` caches what it yields. Iterating the result streams the items, and the assembled list is stored once the producer finishes.

```python title="stream.py"
--8<-- "cache/stream.py"
```

This is the shape where one result is served two ways: a streaming endpoint that yields items as they are produced, and a buffered endpoint that returns the whole thing. Both read one entry, so whichever runs first pays for the work:

```python
@app.get("/answer/{question_id}/stream")
async def stream(question_id: int) -> EventSourceResponse:
    return EventSourceResponse(answer(question_id))


@app.get("/answer/{question_id}")
async def whole(question_id: int) -> str:
    return "".join(await answer.collect(question_id))
```

**Only a completed sequence is stored.** A reader that stops early, and a producer that raises part way, both leave the key untouched. A truncated sequence is never published, so the next reader gets the whole thing rather than the part the first one happened to read.

**A second reader waits, then replays.** Under the default `lock`, a concurrent miss folds like any other: the second caller waits for the first to finish and then replays the stored entry, rather than running the producer again. It trades incremental output for not paying twice. Use `lock=False` to let both stream live at the cost of two executions.

**The stored form is a plain list**, so `await cache.get(key)` returns the items and anything else reading that key sees an ordinary cached value.

`skip` receives the assembled list, so `skip=lambda items: not items` declines to store an empty sequence. `tags`, `early` and `refresh()` work as they do anywhere else, and `stale_ttl` serves the reserve when the producer fails before its first item. Past that the caller already holds part of the live sequence, so the error propagates rather than replaying items it just read.

A sync generator is not supported and raises at decoration time. It yields its items once, so a cached one would replay as empty.

## Stampede Protection

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
async def get_user(user_id: int) -> User:
    return await db.fetch_user(user_id)


@cached(cache, lock=True)       # fold misses, across replicas if a lock backend is set
async def get_billing(user_id: int) -> Billing:
    return await billing.fetch(user_id)


@cached(cache, early=0.1)       # refresh hot keys before they expire
async def get_homepage_feed() -> Feed:
    return await build_feed()
```

`lock` is **per-key**: concurrent misses on different keys run in parallel. Only callers that request the same key wait in turn, so one slow computation does not block unrelated keys.

`lock=True` folds misses across replicas when the active `Grelmicro` app has a `Coordination` backend, and folds them in-process when it does not. Use `lock="local"` to force the in-process path even when a `Coordination` backend is configured.

`early=` returns the cached value immediately and recomputes in the background, so a hot key refreshes before it expires and no caller ever waits on a cold miss. It costs one extra recompute per refresh and stores a small sidecar entry next to the value so replicas coordinate the refresh window.

**When to use:** your cached function is expensive (database query, API call, heavy computation) and may be called concurrently with the same arguments.

## Serve Stale on Error

Set `stale_ttl` to keep serving the last good value when a recompute fails. Each result is also kept as a fallback copy for `ttl + stale_ttl` seconds. After the TTL, the next miss recomputes as usual, but if that recompute raises, the most recent value is served instead of propagating the error, for up to `stale_ttl` seconds past the TTL.

```python
cache = TTLCache[Rates](ttl=60)

@cached(cache, stale_ttl=600)
async def get_exchange_rates() -> Rates:
    return await rates_api.fetch()   # a flaky external call
```

A flaky upstream then degrades to slightly stale data instead of an error storm. Once the recompute succeeds again, the fresh value takes over. If the upstream stays down longer than `stale_ttl`, the error propagates.

`stale_ttl` composes with `lock` and `early`. An explicit `cache.delete(...)` or `cache.delete_tags(...)` drops the fallback too, so invalidation is never undone by a later stale serve. Each stale serve records the `grelmicro.cache.stale_serves` metric, so a rising count signals an unhealthy upstream.

## Decorator Parameters

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
| `lock` | `True`, `False`, or `"local"` | `"local"` | Concurrent-miss (stampede) protection. |
| `early` | `float` in `[0, 1)` | `None` | Probabilistic early refresh in the late TTL window. |
| `stale_ttl` | `float` | `None` | Serve-stale-on-error budget in seconds. Serve the last good value for this long past the TTL when a recompute fails. |
| `tags` | `Sequence[str]` | `()` | Tags to attach to each result. Templates like `"user:{user_id}"` fill in from the arguments. Invalidate with `cache.delete_tags(...)`. |

## Decorated Function Helpers

| Helper | Returns | Description |
|---|---|---|
| `refresh(*args, **kwargs)` | the new value | Recompute for these arguments and overwrite the stored entry. On a method, call it as `Class.method.refresh(obj, ...)`. |
| `collect(*args, **kwargs)` | awaitable list | Read the whole sequence of a streaming producer. Only on an async generator. |
| `cache_info()` | `CacheInfo` | Statistics for the whole cache backing this function. |
| `cache_clear()` | awaitable | Remove every entry from that cache. Always a coroutine, even for a sync function. |
