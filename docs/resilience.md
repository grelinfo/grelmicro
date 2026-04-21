# Resilience Patterns

The `resilience` package provides patterns to improve fault tolerance and reliability in distributed systems.

- **[Circuit Breaker](#circuit-breaker)**: Automatically detects repeated failures and temporarily blocks calls to unstable services, allowing them time to recover.
- **[Rate Limiter](#rate-limiter)**: Limits the number of requests per time window to protect services from overload. Supports multiple algorithms (GCRA, Token Bucket).
- **[Memory Token Bucket](#memory-token-bucket)**: Standalone synchronous primitive (powers [`grelmicro.logging.RateLimitFilter`][grelmicro.logging.RateLimitFilter]).

## Circuit Breaker

A **Circuit Breaker** prevents repeated failures when calling unreliable services. It monitors call outcomes and, after too many consecutive failures, "opens" to block further calls for a period, allowing recovery.

**Why Circuit Breakers?**

- Prevent cascading failures
- Improve stability and user experience
- Provide observability into service health

### State Machine

The Circuit Breaker has three normal states and two manual (forced) states:

| State         | Description                                                        |
|---------------|--------------------------------------------------------------------|
| **CLOSED**        | Normal operation, calls are allowed.                               |
| **OPEN**          | Calls are blocked to allow recovery.                              |
| **HALF_OPEN**     | Allows limited calls to test if the service has recovered.         |
| **FORCED_OPEN**   | Manual state to block calls regardless of health checks.          |
| **FORCED_CLOSED** | Manual state to allow calls regardless of health checks.          |

### Usage

```python
--8<-- "resilience/circuitbreaker.py"
```

!!! warning
    **Thread Safety:** The Circuit Breaker is not thread-safe. Decorated sync functions or `from_thread` methods will ensure state change logic runs safely within the async event loop. Threaded usage is supported only in AnyIO worker threads and may be slower than pure async usage.

## Rate Limiter

A **Rate Limiter** controls how many requests a client can make within a time window. `RateLimiter` is algorithm-agnostic: pass an `algorithm=` instance to choose between implementations. Everything else (the API, the `RateLimitResult`, the backend registry, fail-open) is shared.

**Why Rate Limiting?**

- Protect services from overload and abuse
- Enforce fair usage across clients
- Enable HTTP 429 responses with standard headers

### Choosing an algorithm

| Algorithm | Semantics | Parameters | When to use |
|---|---|---|---|
| **[`GCRA`][grelmicro.resilience.algorithms.GCRA]** | Sliding-window (leaky bucket as meter) | `limit`, `window` | Precise HTTP API throttling with `X-RateLimit-*` headers. Memory-efficient (~72 bytes/key). Mathematically equivalent to the "leaky bucket" formulation (Stripe, Cloudflare). |
| **[`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket]** | Burst + continuous refill | `capacity`, `refill_rate` | Operators reason in "allow a burst of N, then 1/sec" language (Log4j2 BurstFilter, zerolog, AWS API Gateway). |

!!! note
    There is no separate `LeakyBucket` algorithm because GCRA **is** the leaky-bucket-as-meter formulation. Operators searching for "leaky bucket" should use `GCRA`.

### Backend

Load a rate limiter backend before using `RateLimiter`. The same backend serves every algorithm.

=== "Redis"
    ```python
    --8<-- "resilience/ratelimiter_redis.py"
    ```

=== "Memory"
    ```python
    --8<-- "resilience/ratelimiter_memory.py"
    ```

!!! warning
    Please make sure to use a proper way to store connection URLs, such as environment variables (not like the example above).

| | Redis | Memory |
|---|---|---|
| **Use case** | Production | Testing / single-process |
| **Multi-node** | Yes | No |
| **Persistence** | Yes (auto-expiring keys) | No |

The backend compiles the algorithm into a bound strategy at `RateLimiter.__init__` via `backend.bind(algorithm)`. Runtime `acquire`/`peek`/`reset` calls invoke that strategy directly: **no algorithm dispatch on the hot path**.

### Usage

```python
--8<-- "resilience/ratelimiter.py"
```

### Result Fields

`RateLimitResult` is the same across algorithms and provides all the information you need for HTTP rate limit headers:

| Field | Type | Description | HTTP Header |
|---|---|---|---|
| `allowed` | `bool` | Whether the request is permitted | 200 vs 429 status |
| `limit` | `int` | Total quota (`limit` for GCRA, `int(capacity)` for TokenBucket) | `X-RateLimit-Limit` |
| `remaining` | `int` | Remaining requests / tokens | `X-RateLimit-Remaining` |
| `retry_after` | `float` | Seconds until next allowed request | `Retry-After` |
| `reset_after` | `float` | Seconds until full quota resets | `X-RateLimit-Reset` |

### Weighted Requests

Use the `cost` parameter to consume multiple tokens per request:

```python
# Bulk endpoint costs 10 tokens
result = await api_limiter.acquire(key=user_id, cost=10)
```

### Peek (Check Without Consuming)

Use `peek()` to check the current state without consuming tokens:

```python
--8<-- "resilience/ratelimiter_peek.py"
```

### Reset

Use `reset()` to delete the state for a key, restoring its full quota:

```python
--8<-- "resilience/ratelimiter_reset.py"
```

### Fail-Open Mode

Use `fail_open=True` when availability is more important than strictness. On backend errors (e.g. Redis down), the rate limiter returns an allowed result instead of propagating the exception:

```python
--8<-- "resilience/ratelimiter_fail_open.py"
```

!!! warning
    Fail-open mode only catches backend infrastructure errors. Legitimate rate limit rejections still work normally.

!!! tip
    The rate limiter uses the same backend registry pattern as the synchronization primitives. See [Backend Architecture](architecture/backends.md) for details.

### Migration from the legacy constructor

The shorthand `RateLimiter(name, limit=..., window=...)` is still accepted and emits a `DeprecationWarning`: it's internally rewritten to `RateLimiter(name, algorithm=GCRA(limit=..., window=...))`. It will be removed in **0.7.0**.

```python
# Before (deprecated)
RateLimiter("auth", limit=5, window=60)

# After
RateLimiter("auth", algorithm=GCRA(limit=5, window=60))
```

## Memory Token Bucket

`MemoryTokenBucket` is a **standalone, synchronous, thread-safe** in-memory token-bucket primitive. Unlike `RateLimiter`, it's not pluggable and not async: it exists for callers who need a raw, zero-I/O bucket on a sync hot path. It powers [`grelmicro.logging.RateLimitFilter`][grelmicro.logging.RateLimitFilter], which is the recommended way to consume it if you're rate-limiting log records. Use it directly for anything else.

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
