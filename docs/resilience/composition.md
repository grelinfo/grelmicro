# Composing patterns

Resilience patterns wrap a call in layers. Each layer adds its own behavior, and the order of the layers changes what they mean. A timeout outside a retry bounds every attempt together. The same timeout inside the retry bounds one attempt.

A [`Stack`](#stack) takes the patterns and applies them in the safe order, so the order stops being something to get right at every call site.

## The order

```text
Fallback → Retry → CircuitBreaker → RateLimiter → Bulkhead → Timeout → call
```

Read it as the path of one call. The call travels left to right, and the result or the error travels back right to left. Whatever touches `call` is innermost.

| Layer | Reason it sits where it does |
|---|---|
| **Fallback** | Outermost. Catches whatever still escapes the inner layers and returns a safe value. |
| **Retry** | Above the breaker. Retries transient errors, and stops as soon as the breaker refuses. |
| **CircuitBreaker** | Above the limiter. Once the dependency is failing, refusing here spends no tokens and no permits. |
| **RateLimiter** | Above the bulkhead. A call waiting for tokens holds no permit while it waits. |
| **Bulkhead** | Above the timeout. Caps concurrency before the call enters its deadline. |
| **Timeout** | Innermost. Bounds one attempt. |
| **call** | The function. |

## Stack

Pass the patterns in any order. A `Stack` applies them in the order above:

```python
--8<-- "resilience/stack.py"
```

A `Stack` is reusable. Decorate several functions with the same one and they share its breaker, its bucket, and its permits, which is what you want when they call the same dependency.

Two patterns of the same kind are refused, because that needs an order only you know. So is a stack with no patterns, because it would return the function unchanged.

## What a Stack guarantees

A refusal by the stack's own rate limiter, bulkhead, or circuit breaker is not an outcome of the call. The call never happened. Inside a `Stack` those refusals are read that way:

| Refusal by the stack's own | Inside a Stack |
|---|---|
| `CircuitBreakerError` | its `Retry` never retries it |
| `RateLimitExceededError` | its `CircuitBreaker` records no outcome |
| `BulkheadFullError` | its `CircuitBreaker` records no outcome |

Nothing that happens while the stack is admitting a call counts against its breaker either. A `key_maker` that raises, a `cost` above the limiter's capacity, and a limiter backend that is down are all refused without recording a dependency outcome, so a mistake in one tenant's key cannot open the circuit for every caller.

Everything else reaches every pattern exactly as that pattern's own `when=` or `ignore_exceptions` decides. The stack changes no configuration.

The refusal itself is unchanged. A `Fallback` at the top still catches the `CircuitBreakerError`, and a caller still reads `retry_after` off the `RateLimitExceededError`.

This is the one place a `Stack` is more than the hand-written layers below. Stacking the same patterns by hand gives you the raw composition, where a broad `when=` retries through an open breaker and a quota refusal counts as a dependency failure.

## Budgets add up

Each layer holds its own budget, and one attempt can spend all of them in turn. The worst case for a single attempt is the sum:

```text
rate limiter max_wait + bulkhead max_wait + timeout seconds
```

The `Timeout` is innermost, so it bounds the call and nothing above it. A `Bulkhead(max_wait=30.0)` queues for a permit *outside* that deadline, and a limiter with a wait budget queues outside both. To bound one attempt end to end, budget the parts. To bound a whole logical call including its retries, put a deadline above the stack:

```python
async with asyncio.timeout(5.0):
    await get_recommendations(user_id)
```

Both wait budgets default to no waiting at all, so a stack refuses rather than queues until you ask otherwise.

A retry spends the budgets again. Every attempt re-enters the limiter and takes a fresh token, including an attempt the bulkhead then refuses, so a broad `Retry(when=Exception)` above a saturated bulkhead drains the quota on calls that never leave the process. This is the case the [narrow `when=`](#picking-when) advice exists for: the usual `when=httpx.HTTPError` never matches a `BulkheadFullError`, so the default is already right.

## The imperative form

For a call site that cannot be decorated, `run` applies the same stack:

```python
--8<-- "resilience/stack_run.py"
```

`recs.run(fn, *args, **kwargs)` calls `fn(*args, **kwargs)` under the same patterns, so the same breaker, bucket, and permits are shared with the decorated functions.

## Sync functions

`Fallback`, `Retry`, and `CircuitBreaker` wrap `def` functions as well as `async def`. `RateLimiter`, `Bulkhead`, and `Timeout` are async only, so a stack that holds one of them refuses a sync function where it is written:

```text
Stack 'recs' only decorates async functions, because Timeout does.
Make refresh_prices async, or drop that pattern from the stack.
```

A sync stack that holds a `CircuitBreaker` runs from a worker thread. The breaker keeps its state on the backend and reaches it through the event loop the backend runs on, so calling it from that loop would wait on the loop that has to do the work. That is refused with a message rather than left to hang, and the refusal travels the way a cancellation does, past every `Retry` and `Fallback`, so a wiring mistake is never stood in for by a default. Reach it with `asyncio.to_thread(...)`, the same way [`CircuitBreaker`](circuit-breaker.md) is reached from sync code on its own.

An object whose `__call__` is `async def` counts as an async function everywhere a `Stack` looks, in the decorator and in `run`.

## Building the list conditionally

A `None` entry is skipped, the way it is in `Grelmicro(uses=[...])`, so a pattern that applies to one deployment stays a plain expression: `patterns=[retrier, breaker if shared else None]`.

For a list built beforehand, annotate it with `Pattern`:

```python
--8<-- "resilience/stack_conditional.py"
```

## What goes in a Stack

A `Stack` composes the six patterns that wrap a call. Anything else is refused at construction, with where it actually belongs:

| Reached for | Where it goes |
|---|---|
| [`Shield`](shield.md) | On its own. It is already a stack, with its own timeout, retries, and adaptive throttling. |
| [`@cached`](../cache/cached.md) | Above the Stack, so a hit answers without entering it. |
| A task decorator (`@tasks.every`, `@tasks.cron`) | Above the Stack. It registers the function it is handed, so a Stack below it would wrap only direct calls and leave every scheduled run unprotected. Written the wrong way round, the Stack refuses. |
| [`LeaderElection`](../coordination/leader-election.md) | Nowhere in the order. It runs as a service. Gate the work on `leader.is_leader`. |
| [`Lock`](../coordination/lock.md), [`ReadWriteLock`](../coordination/read-write-lock.md) | Inside the function. Both are keyed and held around a block. |
| A generator function | Around the code that consumes it, or around the call inside its body. A generator runs its body while it is iterated, so a Stack would wrap building it and nothing else. Refused. |

Read outside-in, a fully wired call site looks like this:

```python
@tasks.every(seconds=60)
@cached(prices, ttl=30)
@recs
async def refresh_prices() -> list[Price]: ...
```

## Stacking by hand

The order is enforced by `Stack`, not by the decorators. Stack them yourself when you want a different one:

```python
--8<-- "resilience/timeout_composition.py"
```

Read top to bottom: the call enters `Fallback`, which delegates to `Retry`, which delegates to `CircuitBreaker`, which delegates to `Timeout`, which finally runs the function.

Hand-stacked layers get none of the guarantees above, so the filters have to carry them:

- Keep `when=` on the retry narrow. A broad `Retry(when=Exception)` retries a `CircuitBreakerError`, so an open breaker spends every attempt and every backoff before failing.
- Keep the breaker's `ignore_exceptions` aware of admission errors. Without it a full bulkhead or a spent quota counts as a dependency failure and helps open the circuit.

## Picking `when=`

Every pattern uses the same `when=` keyword for its outcome filter, fed by the [`Match`](retry.md#filtering-outcomes-with-match) DSL:

```python
retrier = Retry.exponential("recs", when=httpx.HTTPError, attempts=3)
```

A broad `fallback(when=Exception, default=...)` swallows every error inside the layers below. That is usually what you want at a graceful-degradation boundary. Pair it with a narrower retry so transient errors get a second chance before the fallback fires.
