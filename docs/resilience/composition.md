# Composing patterns

Resilience patterns compose by stacking decorators. Each Pattern wraps the call in its own behavior, and the stack runs outside-in: the outermost decorator runs first, the innermost wraps the actual call.

## Recommended order

```text
Fallback → Retry → CircuitBreaker → Bulkhead → Timeout → call
```

```python
@fallback(when=Exception, default=[])
@retrier
@breaker
async def get_recommendations(...): ...
```

Read top to bottom: the call enters `Fallback`, which delegates to `Retry`, which delegates to `CircuitBreaker`, which finally runs the function.

## Why this order

| Layer | Reason it sits where it does |
|---|---|
| **Fallback** | Outermost. Catches whatever still escapes the inner stack and returns a safe value. |
| **Retry** | Above the breaker. Retries transient errors. When the breaker opens, retry should respect that and not loop on `CircuitBreakerError`. Use a narrow `when=` allowlist. |
| **CircuitBreaker** | Above bulkhead. Once the dependency is failing, blocking calls here saves bulkhead slots and downstream timeouts. |
| **Bulkhead** | Above timeout. Caps concurrency before the call enters the timeout window. |
| **Timeout** | Innermost user-facing pattern. Bounds the actual call. |
| **call** | The function. |

## Picking `when=`

Every Pattern uses the same `when=` keyword for its outcome filter, fed by the [`Match`](retry.md#filtering-outcomes-with-match) DSL. A broad `Retry(when=Exception)` retries through an open breaker. Always narrow the retry filter:

```python
retrier = Retry.exponential("recs", when=httpx.HTTPError, attempts=3)
```

A broad `fallback(when=Exception, default=...)` swallows every error inside the stack. That is usually what you want for a graceful-degradation boundary, but pair it with a narrower retry so transient errors get a second chance before the fallback fires.
