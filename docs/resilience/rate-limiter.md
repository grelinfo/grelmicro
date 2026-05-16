# Rate Limiter

A **Rate Limiter** caps how many requests a client can make inside a time window. `RateLimiter` is algorithm-agnostic. Pass an algorithm config to choose semantics. Everything else (API, `RateLimitResult`, backend registry, `fail_open`) is shared.

**Why**

- Protect services from overload and abuse.
- Enforce fair usage across clients.
- Produce HTTP 429 responses with [RFC 9211](https://www.rfc-editor.org/rfc/rfc9211.html) `RateLimit-*` or legacy `X-RateLimit-*` headers.

## Construction

For day-to-day Python code, use the factory classmethods. They keep the call site explicit and short:

```python
--8<-- "resilience/ratelimiter_factories.py"
```

Use `RateLimiter.from_config(name, config)` when the algorithm config already comes from a settings tree, YAML, or another declarative source.

```python
--8<-- "resilience/ratelimiter_from_config.py"
```

`RateLimiter` intentionally does not flatten both algorithms into one generic kwargs constructor. Token bucket and sliding window have different parameter vocabularies, and keeping one explicit entry point per behaviour makes the public API easier to read.

## Choosing an algorithm

Pick the algorithm whose behaviour matches how **operators describe the limit** in runbooks and API docs. Both algorithms share the same Python API, backends, and `RateLimitResult` shape, so you can switch later.

### Decision guide

1. **Are you throttling an HTTP API with `RateLimit-*` or `X-RateLimit-*` headers?** Use [`SlidingWindowConfig`][grelmicro.resilience.algorithms.SlidingWindowConfig]. It matches the IETF RateLimit headers directly and produces precise `limit`, `remaining`, and `reset_after` values.
2. **Do you want "allow a burst of N, then 1 per second sustained"?** Use [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]. The `capacity` and `refill_rate` parameters describe exactly that.
3. **Does a client need to send occasional spikes above the average rate?** Use [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]. The capacity absorbs the spike.
4. **Did you search for "leaky bucket"?** Use [`SlidingWindowConfig`][grelmicro.resilience.algorithms.SlidingWindowConfig]. It is the leaky-bucket-as-meter formulation.

### Side-by-side

| | **SlidingWindowConfig** | **TokenBucketConfig** |
|---|---|---|
| **Mental model** | "N requests per sliding T-second window" | "A bucket holding N tokens that refills at R tokens/sec" |
| **Parameters** | `limit`, `window` | `capacity`, `refill_rate` |
| **Burst behaviour** | Up to `limit` requests if the window is empty | Up to `capacity` if the bucket is full |
| **Sustained rate** | `limit / window` requests per second | `refill_rate` tokens per second |
| **HTTP header fit** | Strong. `reset_after` is a true window boundary and maps directly to `RateLimit-Reset`. | Workable. `retry_after` is the time until the next token (continuous refill), not a window reset. |

!!! note "Performance"
    Both algorithms run in O(1) per operation. End-to-end latency is dominated by the backend: a Redis round-trip costs far more than the algorithm itself. Per-key memory on the Memory backend differs by about 15 MB per million keys. Choose based on behaviour, not compute cost.

### Worked scenarios

- **"Limit each user to 100 API calls per minute."** Use `SlidingWindowConfig(limit=100, window=60)`. The sliding window matches the natural description, and `RateLimitResult.reset_after` feeds directly into `RateLimit-Reset`.
- **"Allow a burst of 20 uploads, then 2 per second."** Use `TokenBucketConfig(capacity=20, refill_rate=2)`. Each word in the sentence maps to one parameter.
- **"Fair share. Every account gets 1 heavy job per 10 seconds but can queue up to 5."** Use `TokenBucketConfig(capacity=5, refill_rate=0.1)`.
- **"Throttle expensive webhook retries. At most 10 per minute per target."** Use `SlidingWindowConfig(limit=10, window=60)`.

!!! note
    There is no separate `LeakyBucket` algorithm. `SlidingWindowConfig` is the leaky-bucket-as-meter formulation. Operators searching for "leaky bucket" should use `SlidingWindowConfig`.

## Backend

Load a backend before using `RateLimiter`. The same backend serves every algorithm.

!!! tip "Install"
    The Redis backend needs the `redis` extra: `pip install "grelmicro[redis]"`. See the [installation guide](../installation.md) for `uv` and `poetry`.

=== "Redis"
    ```python
    --8<-- "resilience/ratelimiter_redis.py"
    ```

=== "Memory"
    ```python
    --8<-- "resilience/ratelimiter_memory.py"
    ```

!!! warning
    Please make sure to use a proper way to store connection URLs, such as environment variables, not hard-coded strings like the example above.

| | Redis | Memory |
|---|---|---|
| **Use case** | Production | Testing / single-process |
| **Multi-node** | Yes | No |
| **Persistence** | Yes (auto-expiring keys) | No |

The backend compiles the algorithm into a bound strategy at `RateLimiter.__init__` through `backend.bind(config)`. Runtime `acquire`, `peek`, and `reset` calls invoke that strategy directly. **There is no algorithm dispatch on the request path.**

## Usage

```python
--8<-- "resilience/ratelimiter.py"
```

### Result fields

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

!!! tip
    The rate limiter uses the same backend registry pattern as the synchronization primitives. See [Backend Architecture](../architecture/backends.md) for details.

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
