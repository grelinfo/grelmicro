# Resilience

The `resilience` package provides primitives that help your services handle failures in distributed systems. Each pattern is independent. Use the ones you need and skip the rest.

- [**Shield**](shield.md): all-in-one outer-layer resilience for one async call to one dependency. Bundles per-attempt timeout, exponential-jittered retry with a consecutive-failure budget, and CUBIC-style adaptive throttling. One decorator, one knob. The recommended starting point.
- [**Circuit Breaker**](circuit-breaker.md): detect repeated failures in a downstream call and stop sending requests long enough for it to recover. This prevents cascading failures.
- [**Rate Limiter**](rate-limiter.md): cap how many requests a client can make per window. Pluggable algorithm (`TokenBucketConfig` or `GCRAConfig`), pluggable backend (`Memory` or `Redis`), with a result shape that maps directly to HTTP rate limit headers.
- [**Retry**](retry.md): repeat a failing call with backoff and jitter. Pluggable algorithm (`ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, `RandomBackoff`), required exception filter, decorator and block forms.

## When to use what

| Pattern | Use when |
|---|---|
| **Shield** | You wrap one async call to one dependency and want sensible-by-default resilience without composing primitives. |
| **Circuit Breaker** | The caller is hitting an external dependency (DB, API, third-party) that can fail or stall. |
| **Rate Limiter** | You need to throttle *your own* endpoint, worker, or background job to protect the downstream side or enforce fair usage. |
| **Retry** | A call fails for transient reasons (network blip, brief overload) and is safe to repeat. |

For synchronous, in-process token-bucket rate limiting on a performance-critical sync path (the main example is the logging pipeline), see [`MemoryTokenBucket`](rate-limiter.md#standalone-memorytokenbucket). It powers `grelmicro.log.RateLimitFilter`.
