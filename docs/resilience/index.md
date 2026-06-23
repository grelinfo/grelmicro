# Resilience

The `resilience` package provides primitives that help your services handle failures in distributed systems. Each pattern is independent. Use the ones you need and skip the rest.

- [**Shield**](shield.md): all-in-one outer-layer resilience for one async call to one dependency. Bundles per-attempt timeout, exponential-jittered retry with a consecutive-failure budget, and CUBIC-style adaptive throttling. One decorator, one knob. The recommended starting point.
- [**Circuit Breaker**](circuit-breaker.md): detect repeated failures in a downstream call and stop sending requests long enough for it to recover. This prevents cascading failures.
- [**Fallback**](fallback.md): return a safe default value when a call fails. Pluggable filter (any `Match`), static default or factory callable, decorator and block forms.
- [**Rate Limiter**](rate-limiter.md): cap how many requests a client can make per window. Pluggable algorithm (`TokenBucketConfig` or `SlidingWindowConfig`), pluggable backend (`Memory`, `Redis`, `Postgres`, or `SQLite`), with a result shape that maps directly to HTTP rate limit headers.
- [**Retry**](retry.md): repeat a failing call with backoff and jitter. Pluggable algorithm (`ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, `RandomBackoff`), required exception filter, decorator and block forms.
- [**Timeout**](timeout.md): bound how long an async call may run. Reconfigurable deadline, async context manager and decorator forms. Wraps `asyncio.timeout`.
- [**Bulkhead**](bulkhead.md): cap concurrent in-flight calls and fail fast when saturated. Optional bounded wait for a permit and a dedicated thread pool for blocking work. Async context manager and decorator forms.

See [Composing patterns](composition.md) for the recommended outside-in order when stacking decorators.

## When to use what

| Pattern | Use when |
|---|---|
| **Shield** | You wrap one async call to one dependency and want sensible-by-default resilience without composing primitives. |
| **Circuit Breaker** | The caller is hitting an external dependency (DB, API, third-party) that can fail or stall. |
| **Fallback** | A call can fail and you want to degrade gracefully (cached value, empty list, neutral default) instead of propagating the error. |
| **Rate Limiter** | You need to throttle *your own* endpoint, worker, or background job to protect the downstream side or enforce fair usage. |
| **Retry** | A call fails for transient reasons (network blip, brief overload) and is safe to repeat. |
| **Timeout** | An async call may stall and you need a hard deadline on it. |
| **Bulkhead** | A dependency can saturate your workers and you want to cap concurrent calls and fail fast instead of queueing unboundedly. |

For synchronous, in-process token-bucket rate limiting on a performance-critical sync path (the main example is the logging pipeline), see [`MemoryTokenBucket`](rate-limiter.md#standalone-memorytokenbucket). It powers `grelmicro.log.RateLimitFilter`.

## The AdmissionError family

The gatekeeping primitives refuse a call when they turn it away. Each refusal subclasses `AdmissionError`, so one `except` catches them all:

| Error | Raised by |
|---|---|
| `WouldBlockError` | a non-blocking `Lock` acquire that would have blocked |
| `BulkheadFullError` | a `Bulkhead` with no free permit |
| `RateLimitExceededError` | a `RateLimiter` over budget |
| `CircuitBreakerError` | an open `CircuitBreaker` |

```python
from grelmicro.errors import AdmissionError

try:
    ...
except AdmissionError:
    # turned away by a lock, bulkhead, rate limiter, or circuit breaker
    ...
```

## With FastStream

The same primitives drop into a FastStream consumer without changes.
The lifespan opens the shared Redis provider, `Coordination`, and
`RateLimiterRegistry` once. A handler can then hold a per-key `Lock` and
consume rate-limit tokens before the actual work runs.

```python
--8<-- "resilience/faststream.py"
```

The lock and limiter are fleet-wide: every consumer replica sees the
same budget per key.
