# Shield

A **Shield** wraps one async call to one dependency with resilience built in: a per-attempt timeout, automatic retries, and an adaptive rate limiter that engages only when the dependency starts to struggle. One decorator, one knob.

**Why**

- Wrap one call to one dependency without composing four primitives by hand.
- Survive transient slow-downs (network blips, brief overloads) without touching the call site.
- Pace the whole client down automatically when a dependency keeps timing out, then ramp back up gradually.
- Stay invisible on the happy path. The adaptive layer turns itself on only after the first slow-down.

## Usage

Decorate an async function, tell Shield which exceptions mean "the dependency is slow", and call it as usual:

```python
--8<-- "resilience/shield.py"
```

The zero-argument form uses the `api` profile and treats `TimeoutError` as the slow signal:

```python
--8<-- "resilience/shield_zero_args.py"
```

Shield is **async-only**. Use it on coroutine functions.

## Choose a profile

A profile sets the starting rate and timeout for the kind of dependency you are calling. Pick one and you are done.

| Profile | Use when | Initial rate (per second) | Initial timeout |
|---|---|---|---|
| `internal` | In-cluster RPC. Healthy services, fast latency, tight budgets. | `100` | `1.0s` |
| `api` (default) | External HTTP APIs. Moderate latency, occasional outages, third-party SLAs. | `2` | `10.0s` |
| `slow` | Long-running calls: LLM inference, batch jobs, large queries. | `0.5` | `120.0s` |

```python
--8<-- "resilience/shield_profiles.py"
```

Profiles are mutually exclusive: pick one per `Shield` instance. To pace differently, switch profile instead of tuning individual fields. If none of the three fit, you have outgrown Shield: compose `Retry`, `Timeout`, `RateLimiter`, and `CircuitBreaker` directly instead.

## Sharing across functions

Build a `Shield` instance once and decorate several functions with it. They share one retry budget and one adaptive controller, which is what you want when those functions hit the same dependency:

```python
--8<-- "resilience/shield_class.py"
```

One `Shield` instance per logical dependency. Two functions hitting GitHub share one budget. Add Stripe and you get a second `Shield`.

The `name=` argument is the registration name used in logs, metrics, and give-up notes. It defaults to the wrapped function's `__qualname__` with `@shield.api(...)`, and is a required positional argument with `Shield.api(...)`.

## Imperative form

For inline calls that span several statements, use `Shield.run`:

```python
--8<-- "resilience/shield_run.py"
```

`Shield.run(fn, *args, **kwargs)` calls `fn(*args, **kwargs)` under the same retry-budget and adaptive state as the decorator form.

## Cache and fallback

When Shield gives up, it tries two recovery paths in order: a cache lookup, then a fallback callable. Set either or both.

### Cached fallback

Pass a `Cache` instance to `cache=`. Shield writes the return value on every success and reads it back on give-up:

```python
--8<-- "resilience/shield_cache.py"
```

- **On success**: `await cache.set(key, return_value)` runs fire-and-forget. A write failure is logged at debug and never propagates.
- **On give-up**: `await cache.get(key)` runs. A hit returns the cached value. A miss continues to the next recovery path.
- **Key**: `f"{shield.name}:{stable_hash(args, kwargs)}"` by default. Override it with `cache_key=`:

```python
--8<-- "resilience/shield_cache_key.py"
```

Non-hashable arguments (Pydantic models, dataclasses) are hashed via a stable `repr()`.

### Custom fallback

Pass a callable to `fallback=` for when the cache misses (or no cache is set). It receives the exception that escaped Shield:

```python
--8<-- "resilience/shield_fallback.py"
```

### Recovery order on give-up

| Step | Condition | Outcome |
|---|---|---|
| 1 | `cache=` set and cache hit | return the cached value |
| 2 | `cache=` unset or cache miss, `fallback=` set | call `fallback(exc)`, return its result |
| 3 | neither path returns a value | re-raise the original exception with a [PEP 678](https://peps.python.org/pep-0678/) note |

Use `cache=` alone when stale data is acceptable. Add `fallback=` as a safety net for a cold cache. Use `fallback=` alone for synthesized defaults.

## Per-call rate ceiling

If the dependency has a contractual quota (an SLA limit, a third-party rate limit), set `max_rate=` to cap the adaptive limiter's growth:

```python
--8<-- "resilience/shield_max_rate.py"
```

The rate still grows back after recovery, but never past `max_rate`. Without it, the ceiling grows unbounded while the dependency stays healthy.

## Which exceptions are retried

Shield decides what to do by the type of exception the wrapped call raises.

| Raised by the wrapped call | Retried? | Slows the rate? | Consumes retry budget? | Cache / fallback recovery? |
|---|---|---|---|---|
| Any type in `timeout_errors` (or a subclass) | yes | yes | yes | yes, on give-up |
| Any other `Exception` | no | no | no | yes, on give-up |
| `ResilienceError` subclass | no | no | no | no, propagates immediately |
| `BaseException` outside `Exception` (`KeyboardInterrupt`, `CancelledError`, `SystemExit`) | no | no | no | no, propagates immediately |

You declare what "transient" means by passing exception types to `timeout_errors=`. Any other `Exception` skips the retry loop and goes straight to recovery: if a cache or fallback returns a value, that value is returned, otherwise the original exception is re-raised. `ResilienceError` and `BaseException`-outside-`Exception` always propagate unchanged.

`TimeoutError` is always retryable, even if you do not list it. Shield's own per-attempt timeout raises it, and that signal must always be caught. The effective tuple is `your_types + (TimeoutError,)`.

## Behavior on giving up

When Shield gives up and no recovery path returns a value, the original exception is re-raised with a [PEP 678](https://peps.python.org/pep-0678/) note attached:

```python
--8<-- "resilience/shield_giveup.py"
```

The note records the give-up reason (`budget exhausted`, `attempts exhausted`, `non-retryable exception`), the attempt count, the elapsed time, and the profile name. Callers catch the original exception type, unchanged. There is no `ShieldError` wrapper.

## Composing with client-side retries

Shield is the **outer** layer of resilience. Many client libraries ship their own retries, tuned for protocol-level transience (`Retry-After` headers, idempotency keys, modeled status codes). Shield does not replace that work. It adds a slower layer on top.

You do not need to disable the client's retries. Pass the client's terminal exception types via `timeout_errors=`:

```python
--8<-- "resilience/shield_layered.py"
```

When the inner layer exhausts its own attempts and surfaces an exception, Shield sees it. The adaptive layer engages only when the *outer* exception escapes, never on per-attempt blips the inner layer already handled.

## What Shield does not do

- **HTTP status sniffing.** No `Retry-After` parsing, no 429/503 awareness. Call `response.raise_for_status()` inside the wrapped function and pass the resulting exception type via `timeout_errors=`.
- **Sync callables.** Async-only in 1.0. Wrap sync code in `asyncio.to_thread(...)` if needed.
- **Hedged requests.** Firing N parallel attempts and taking the first success is a different primitive, not on the roadmap.
- **Distributed retry budget.** Each `Shield` is per-process. There is no cross-replica coordination.
- **Total deadline propagation.** Shield enforces per-attempt timeouts. Wrap the whole call in `asyncio.timeout(...)` for a total deadline.

??? note "How the adaptive layer works"

    You never tune these numbers. They are fixed by profile choice and shown here so the behavior is not a black box.

    - **Per-attempt timeout.** Tracks the p95 of the last 32 successful latencies, multiplied by `2.5`, clamped to the profile range. A dependency that gets slower raises its own timeout, up to the profile ceiling.
    - **Retry gate.** A consecutive-failure budget with refund-on-recovery. Each give-up consumes budget, each success refunds it. When the budget is empty, Shield stops retrying instead of hammering a dead dependency.
    - **Adaptive rate limiter.** A CUBIC-style controller from [RFC 9438](https://datatracker.ietf.org/doc/rfc9438/) with `C = 0.4` and `β = 0.7`. It shrinks the allowed rate when slow-downs persist, then grows it back along the CUBIC curve as the dependency recovers. It stays at the profile's full rate until the first sustained slow-down, so the happy path pays nothing.

## Configuration

Pass the profile and knobs as keyword arguments.

```python
--8<-- "resilience/shield_programmatic.py"
```

### Environment variables

Prefix: `GREL_SHIELD_{NAME_UPPER}_`

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_SHIELD_{NAME_UPPER}_PROFILE` | profile | `internal` / `api` / `slow` | `api` |
| `GREL_SHIELD_{NAME_UPPER}_TIMEOUT_ERRORS` | `timeout_errors` | CSV of FQN strings (e.g. `httpx.TimeoutException,httpx.ConnectError`) | `builtins.TimeoutError` |
| `GREL_SHIELD_{NAME_UPPER}_MAX_RATE` | `max_rate` | `float` or empty | unset |

The `cache`, `cache_key`, and `fallback` arguments cannot come from env. Pass them as keyword arguments.

```python
--8<-- "resilience/shield_environmental.py"
```

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition, see [Declarative configuration](../advanced/config.md).

## Live reconfiguration

`Shield` inherits `Reconfigurable[ShieldConfig]`. Calling `shield.reconfigure(new_config)` swaps the snapshot for future calls. An in-flight `await shield.run(...)` keeps its snapshot until it completes. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Shield) for every option.
