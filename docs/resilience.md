# Resilience Patterns

The `resilience` package provides patterns to improve fault tolerance and reliability in distributed systems.

- **[Circuit Breaker](#circuit-breaker)**: Automatically detects repeated failures and temporarily blocks calls to unstable services, allowing them time to recover.
- **[Rate Limiter](#rate-limiter)**: Limits the number of requests per time window to protect services from overload.

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

A **Rate Limiter** controls how many requests a client can make within a time window. It uses the GCRA (Generic Cell Rate Algorithm), which is memory-efficient (~72 bytes per key) and provides exact accuracy with no window boundary issues.

**Why Rate Limiting?**

- Protect services from overload and abuse
- Enforce fair usage across clients
- Enable HTTP 429 responses with standard headers

### Backend

You must load a rate limiter backend before using the rate limiter.

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
| **Use case** | Production | Testing only |
| **Multi-node** | Yes | No |
| **Persistence** | Yes (auto-expiring keys) | No |

### Usage

```python
--8<-- "resilience/ratelimiter.py"
```

### Result Fields

The `RateLimitResult` provides all information needed for HTTP rate limit headers:

| Field | Type | Description | HTTP Header |
|---|---|---|---|
| `allowed` | `bool` | Whether the request is permitted | 200 vs 429 status |
| `limit` | `int` | Total quota for the window | `X-RateLimit-Limit` |
| `remaining` | `int` | Remaining requests in the window | `X-RateLimit-Remaining` |
| `retry_after` | `float` | Seconds until next allowed request | `Retry-After` |
| `reset_after` | `float` | Seconds until full quota resets | `X-RateLimit-Reset` |

### Weighted Requests

Use the `cost` parameter to consume multiple tokens per request:

```python
# Bulk endpoint costs 10 tokens
result = await api_limiter.acquire(key=user_id, cost=10)
```

### Peek (Check Without Consuming)

Use `peek()` to check the current rate limit state without consuming tokens:

```python
--8<-- "resilience/ratelimiter_peek.py"
```

### Reset

Use `reset()` to delete the rate limit state for a key, restoring its full quota:

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
