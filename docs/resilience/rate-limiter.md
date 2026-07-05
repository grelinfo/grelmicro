# Rate Limiter

A **Rate Limiter** caps how many requests a client can make inside a time window. Use it to protect a service from overload and to enforce fair usage across clients.

**Why**

- Protect services from overload and abuse.
- Enforce fair usage across clients.
- Produce HTTP 429 responses with [RFC 9211](https://www.rfc-editor.org/rfc/rfc9211.html) `RateLimit-*` or legacy `X-RateLimit-*` headers.

`RateLimiter` is algorithm-agnostic. Pass an algorithm config to choose semantics. Everything else (API, `RateLimitResult`, backend registry, `fail_open`) is shared.

## Usage

Load a backend, build a limiter with a factory classmethod, then call `acquire`:

```python
--8<-- "resilience/ratelimiter.py"
```

### Checking the limit

Pick the call by what you need:

| Need | Method | Why |
|---|---|---|
| Just branch yes or no | `allow()` | Smallest code. |
| Build HTTP 429 headers | `acquire()` | Keeps `retry_after`, `remaining`, and `reset_after`. |
| Let a shared handler map rejections | `acquire_or_raise()` | Raises `RateLimitExceededError`, an `AdmissionError`. |
| Smooth work instead of rejecting | `wait()` | Sleeps until admitted, with optional `max_wait`. |

The simplest form is a boolean:

```python
if await limiter.allow(key="user-1"):
    ...  # served
else:
    ...  # throttled
```

`acquire` returns the full `RateLimitResult` when you need the metadata. It reads as a boolean too, so you branch on it directly and still keep `retry_after`/`remaining` on the deny side:

```python
result = await limiter.acquire(key="user-1")
if not result:
    # reject with a 429 and a Retry-After of result.retry_after seconds
    ...
```

Use `acquire_or_raise` when a surrounding layer should turn the rejection into a response: it raises `RateLimitExceededError`, which is an `AdmissionError` (the shared base, exported from the top-level `grelmicro` package for every "turned away" rejection: rate limiter, bulkhead, open circuit breaker, non-blocking lock), so one `except AdmissionError` catches them all.

### Waiting until allowed

`wait` blocks until tokens are available, then consumes them. Use it to smooth a burst into the limit instead of rejecting it, for example when calling a rate-limited upstream:

```python
--8<-- "resilience/ratelimiter_wait.py"
```

It polls `acquire` on the clock seam, sleeping `retry_after` between attempts, so a denied call never consumes tokens. By default it waits as long as needed. Pass `max_wait` to bound the wait: it raises `RateLimitExceededError` once the budget would be exceeded. A `cost` larger than the limit raises `ValueError` instead of waiting forever.

### One fleet-wide limit

When the limiter protects a service with one shared budget (no per-user or per-IP split), omit `key`. It defaults to `"default"`, and the limiter's own `name` already namespaces the bucket on the backend:

```python
api_limiter = RateLimiter.token_bucket("api", capacity=5, refill_rate=1)

await api_limiter.acquire()             # one fleet-wide bucket
await api_limiter.allow()
await api_limiter.acquire_or_raise()

await api_limiter.acquire(key=user_id)  # per-subject stays explicit
```

The factory classmethods keep the call site explicit and short:

```python
--8<-- "resilience/ratelimiter_factories.py"
```

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition, see [Declarative configuration](../advanced/config.md).

`RateLimiter` intentionally does not flatten both algorithms into one generic kwargs constructor. Token bucket and sliding window have different parameter vocabularies, and keeping one explicit entry point per behaviour makes the public API easier to read.

## Choosing an algorithm

Pick the algorithm whose behaviour matches how **operators describe the limit** in runbooks and API docs. Both algorithms share the same Python API, backends, and `RateLimitResult` shape, so you can switch later.

- Throttling an HTTP API with `RateLimit-*` headers? Use [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig].
- Want "allow a burst of N, then 1 per second sustained"? Use [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig].

??? note "Decision guide, side-by-side, and worked scenarios"

    ### Decision guide

    1. **Are you throttling an HTTP API with `RateLimit-*` or `X-RateLimit-*` headers?** Use [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]. It matches the IETF RateLimit headers directly and produces precise `limit`, `remaining`, and `reset_after` values.
    2. **Do you want "allow a burst of N, then 1 per second sustained"?** Use [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]. The `capacity` and `refill_rate` parameters describe exactly that.
    3. **Does a client need to send occasional spikes above the average rate?** Use [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]. The capacity absorbs the spike.
    4. **Did you search for "leaky bucket"?** Use [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]. It is the leaky-bucket-as-meter formulation.

    ### Side-by-side

    | | **SlidingWindowConfig** | **TokenBucketConfig** |
    |---|---|---|
    | **Mental model** | "N requests per sliding T-second window" | "A bucket holding N tokens that refills at R tokens/sec" |
    | **Parameters** | `limit`, `window` | `capacity`, `refill_rate` |
    | **Burst behaviour** | Up to `limit` requests if the window is empty | Up to `capacity` if the bucket is full |
    | **Sustained rate** | `limit / window` requests per second | `refill_rate` tokens per second |
    | **HTTP header fit** | Strong. `reset_after` is a true window boundary and maps directly to `RateLimit-Reset`. | Workable. `retry_after` is the time until the next token (continuous refill), not a window reset. |

    ### Worked scenarios

    - **"Limit each user to 100 API calls per minute."** Use `SlidingWindowConfig(limit=100, window=60)`. The sliding window matches the natural description, and `RateLimitResult.reset_after` feeds directly into `RateLimit-Reset`.
    - **"Allow a burst of 20 uploads, then 2 per second."** Use `TokenBucketConfig(capacity=20, refill_rate=2)`. Each word in the sentence maps to one parameter.
    - **"Fair share. Every account gets 1 heavy job per 10 seconds but can queue up to 5."** Use `TokenBucketConfig(capacity=5, refill_rate=0.1)`.
    - **"Throttle expensive webhook retries. At most 10 per minute per target."** Use `SlidingWindowConfig(limit=10, window=60)`.

    There is no separate `LeakyBucket` algorithm. `SlidingWindowConfig` is the leaky-bucket-as-meter formulation. Operators searching for "leaky bucket" should use `SlidingWindowConfig`.

!!! note "Performance"
    Both algorithms run in O(1) per operation. End-to-end latency is dominated by the backend: a Redis round-trip costs far more than the algorithm itself. Per-key memory on the Memory backend differs by about 15 MB per million keys. Choose based on behaviour, not compute cost.

## Backend

Load a backend before using `RateLimiter`. The same backend serves every algorithm.

!!! tip "Install"
    The Redis backend needs the `redis` extra, the Postgres backend needs the `postgres` extra, and the SQLite backend needs the `sqlite` extra: `pip install "grelmicro[redis]"`, `pip install "grelmicro[postgres]"`, or `pip install "grelmicro[sqlite]"`. See the [installation guide](../installation.md) for `uv` and `poetry`.

=== "Redis"
    ```python
    --8<-- "resilience/ratelimiter_redis.py"
    ```

=== "Postgres"
    ```python
    --8<-- "resilience/ratelimiter_postgres.py"
    ```

=== "SQLite"
    ```python
    --8<-- "resilience/ratelimiter_sqlite.py"
    ```

=== "Memory"
    ```python
    --8<-- "resilience/ratelimiter_memory.py"
    ```

!!! warning
    Please make sure to use a proper way to store connection URLs, such as environment variables, not hard-coded strings like the example above.

| | Redis | Postgres | SQLite | Memory |
|---|---|---|---|---|
| **Use case** | Production | Production (when Postgres is already deployed) | Single host that needs durability | Testing / single-process |
| **Multi-node** | Yes | Yes | No | No |
| **Persistence** | Yes (auto-expiring keys) | Yes (table-backed) | Yes (file-backed) | No |

### Choosing a backend

Use **Redis** in production when you already run Redis and want the lowest-latency distributed limiter. Use **Postgres** when Postgres is your only stateful dependency and you want one fewer service to run. Use **SQLite** for a single host that needs limits to survive restarts. Use **Memory** for tests and single-process apps. Redis and Postgres coordinate across replicas. SQLite and Memory do not.

??? note "How each backend stores state"

    The Postgres adapter stores state in a single `grelmicro_rate_limiter` table. `acquire` and `peek` each run one round-trip to a PL/pgSQL function. Concurrent writes for the same key are serialized with `pg_advisory_xact_lock`. `reset` clears the key with a plain `DELETE`. The table and functions are created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema.

    SQLite uses a `SQLiteProvider`, the same provider-first shape as Redis and Postgres. Pass the path to the provider or set the `SQLITE_PATH` environment variable. State lives in a single `grelmicro_rate_limiter` table in the file. Each `acquire` runs a read-modify-write inside a `BEGIN IMMEDIATE` transaction. The provider's lock serializes the single connection within the process, and the transaction's write lock serializes across processes sharing the file. Use it for a single host that wants durability without running a separate service.

    The backend compiles the algorithm into a bound strategy at `RateLimiter.__init__` through `backend.bind(config)`. Runtime `acquire`, `peek`, and `reset` calls invoke that strategy directly. **There is no algorithm dispatch on the request path.**

!!! tip
    The rate limiter uses the same backend registry pattern as the synchronization primitives. See [Backend Architecture](../architecture/backends.md) for details.

!!! note "Coming from 0.x: register a backend, then `install` the app"
    In 0.x you opened a global backend in the lifespan (`async with RedisRateLimiterBackend(...)`). In 1.0 you register a `RateLimiterRegistry` on the app and wire the app with `micro.install(app)`:

    ```python
    micro = Grelmicro(uses=[RateLimiterRegistry(RedisRateLimiterAdapter())])
    micro.install(app)  # opens the registry AND binds it per request
    ```

    `install` is the important part. A module-level `RateLimiter("auth")` resolves its backend from the active app per request, which only works when `install` adds its middleware. Open `async with micro:` in a hand-written lifespan without `install` and the app starts up healthy, then raises `OutOfContextError` on the first rate-limited request. See [Wiring an App](../wiring.md) for the guard and the `micro.check_ambient_binding(app)` test helper.

## Result fields

`RateLimitResult` is the same across algorithms and carries everything needed for HTTP rate limit headers. The `HTTP header` column shows the [RFC 9211](https://www.rfc-editor.org/rfc/rfc9211.html) name first and the legacy `X-RateLimit-*` name second. Pick whichever convention your API already uses.

| Field | Type | Description | HTTP Header |
|---|---|---|---|
| `allowed` | `bool` | Whether the request is permitted | 200 vs 429 status |
| `limit` | `int` | Total quota (`limit` for SlidingWindowConfig, `int(capacity)` for TokenBucketConfig) | `RateLimit-Limit` / `X-RateLimit-Limit` |
| `remaining` | `int` | Remaining requests / tokens | `RateLimit-Remaining` / `X-RateLimit-Remaining` |
| `retry_after` | `float` | Seconds until next allowed request | `Retry-After` |
| `reset_after` | `float` | Seconds until full quota resets | `RateLimit-Reset` / `X-RateLimit-Reset` |

### Weighted requests

Use the `cost` parameter to consume multiple tokens per request.

```python
# Bulk endpoint costs 10 tokens
result = await api_limiter.acquire(key=user_id, cost=10)
```

### Peek (check without consuming)

Use `peek()` to inspect current state without consuming tokens.

```python
--8<-- "resilience/ratelimiter_peek.py"
```

### Reset

Use `reset()` to delete the state for a key, restoring its full quota.

```python
--8<-- "resilience/ratelimiter_reset.py"
```

### Fail-open mode

Use `fail_open=True` when availability matters more than strictness. On backend errors (e.g. Redis down), the rate limiter returns an allowed result instead of raising.

```python
--8<-- "resilience/ratelimiter_fail_open.py"
```

!!! warning
    Fail-open mode only catches backend infrastructure errors. Legitimate rate-limit rejections still work normally.

## Standalone `MemoryTokenBucket`

`MemoryTokenBucket` is a **standalone, synchronous, thread-safe** in-memory token-bucket primitive. Unlike `RateLimiter`, it is not pluggable and not async. Use it when you need a raw, zero-I/O bucket on a synchronous performance-critical path. It powers [`grelmicro.log.RateLimitFilter`][grelmicro.log.RateLimitFilter], which is the recommended way to use it for rate-limiting log records. Call it directly for any other use case.

### Usage

```python
--8<-- "resilience/memory_token_bucket.py"
```

### API

| Method | Description |
|---|---|
| `try_acquire(key="", *, cost=1.0) -> bool` | Consume `cost` tokens and return `True` if allowed. |
| `peek(key="") -> float` | Current token count (fractional). |
| `reset(key="") -> None` | Restore `key` to full capacity. |
| `capacity` / `refill_rate` | Read-only configuration. |
