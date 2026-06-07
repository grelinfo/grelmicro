# Shield

A **Shield** is the outer layer of resilience around one async call to one dependency. It bundles a per-attempt timeout, exponential-jittered retry gated by a consecutive-failure budget, and a CUBIC-style adaptive rate limiter that engages only after sustained slow-downs. One decorator, one knob.

**Why**

- Wrap one call to one dependency without composing four primitives by hand.
- Survive transient slow-downs (network blips, brief overloads) without changing the call site.
- Pace the whole client down automatically when a dependency keeps timing out, then ramp back gradually.
- Stay invisible on the happy path. The adaptive layer enables itself only after the first slow-down.

The adaptive layer uses a CUBIC-style controller from [RFC 9438](https://datatracker.ietf.org/doc/rfc9438/) with `C = 0.4` and `β = 0.7`. The per-attempt timeout tracks the p95 of the last 32 successful latencies multiplied by `2.5`, clamped to the profile range. Retries are gated by a consecutive-failure budget with refund-on-recovery.

## Usage

```python
--8<-- "resilience/shield.py"
```

Pick a profile factory (`shield.internal`, `shield.api`, `shield.slow`), tell Shield which exceptions mean "the dependency is slow", done. Everything else propagates unchanged.

The zero-argument form uses the `api` profile with the default `timeout_errors=(TimeoutError,)`:

```python
--8<-- "resilience/shield_zero_args.py"
```

Shield is **async-only**. Use it on coroutine functions.

## Choose a profile

| Profile | Use when | Initial rate (per second) | Initial timeout |
|---|---|---|---|
| `internal` | In-cluster RPC. Healthy services, fast latency, tight budgets. | `100` | `1.0s` |
| `api` (default) | External HTTP APIs. Moderate latency, occasional outages, third-party SLAs. | `2` | `10.0s` |
| `slow` | Long-running calls: LLM inference, batch jobs, large queries. | `0.5` | `120.0s` |

Three profiles cover the common cases. They are mutually exclusive: pick one per `Shield` instance. To pace differently, switch profile, do not tune individual fields. The internal parameters (retry budget capacity, CUBIC curve constants, backoff scale, timeout clamps) are fixed by profile choice.

```python
--8<-- "resilience/shield_profiles.py"
```

If the three profiles do not fit, you have outgrown Shield. Compose `Retry`, `Timeout`, `RateLimiter` and `CircuitBreaker` directly instead.

## Exception classification

Shield classifies the wrapped call's outcome by exception type only. There is no response envelope, no status code, no `Retry-After` header.

| Raised by the wrapped call | Retried? | Triggers CUBIC shrink? | Consumes retry budget? | Cache / fallback recovery? |
|---|---|---|---|---|
| Any type in `timeout_errors` (or its subclasses) | yes | yes | yes | yes, on give-up |
| Any other `Exception` subclass | no | no | no | yes, on give-up |
| Subclass of `ResilienceError` | no | no | no | no, propagates immediately |
| `BaseException` outside `Exception` (`KeyboardInterrupt`, `CancelledError`, `SystemExit`, `BaseExceptionGroup`) | no | no | no | no, propagates immediately |

You declare what "transient" means by passing exception types to `timeout_errors=`. Any other `Exception` skips the retry loop and goes straight to the [give-up recovery path](#recovery-order-on-give-up). If a cache or fallback returns a value, that value is returned. Otherwise the original exception is re-raised with a [PEP 678](https://peps.python.org/pep-0678/) note. `ResilienceError` and `BaseException`-outside-`Exception` always propagate unchanged with no recovery and no note.

The effective tuple is always `user_tuple + (TimeoutError,)`. Shield's own per-attempt timeout raises `TimeoutError`, and the library guarantees that signal is always retryable, regardless of what the user passed.

The default when the argument is omitted is `(TimeoutError,)`, which covers the per-attempt timeout and any standard Python timeout the wrapped callable raises.

## Sharing across functions

Build a `Shield` instance once and decorate multiple functions with it. They share one retry budget and one CUBIC controller, which is the correct topology when several functions hit the same dependency.

```python
--8<-- "resilience/shield_class.py"
```

One `Shield` instance per logical dependency. Two functions hitting GitHub share one budget. Two functions hitting GitHub and Stripe get two `Shield` instances.

The `name=` argument is the registration name used in logs, metrics, and the [PEP 678](https://peps.python.org/pep-0678/) notes attached on give-up. It defaults to the wrapped function's `__qualname__` when used as `@shield.api(...)`, and is required positional when used as `Shield.api(...)`.

## Imperative form

For inline calls that span multiple statements, use `Shield.run`:

```python
--8<-- "resilience/shield_run.py"
```

`Shield.run(fn, *args, **kwargs)` calls `fn(*args, **kwargs)` under the same retry-budget and adaptive-bucket state as the decorator form.

## Cache and fallback

On give-up (retry budget exhausted, attempts cap reached, or non-retryable exception), Shield tries two recovery paths in order: a cache lookup, then a fallback callable. Either or both can be set.

### Cached fallback

Pass a `Cache` instance to `cache=`. Shield writes the return value on every success and reads it on give-up:

```python
--8<-- "resilience/shield_cache.py"
```

Behavior:

- **On success**: `await cache.set(key, return_value)` runs fire-and-forget. A cache write failure is logged at debug and never propagates.
- **On give-up**: `value = await cache.get(key)` runs. A cache hit returns the cached value. A cache miss continues to the next recovery path.
- **Key**: `f"{shield.name}:{stable_hash(args, kwargs)}"` by default. Override with `cache_key=` for control over what the key looks like:

```python
--8<-- "resilience/shield_cache_key.py"
```

Non-hashable arguments (Pydantic models, dataclasses, etc.) are hashed via stable `repr()`. Override `cache_key=` for non-default behavior.

### Custom fallback

Pass a callable to `fallback=` for the case where the cache misses (or no cache is set). The callable receives the exception that escaped Shield:

```python
--8<-- "resilience/shield_fallback.py"
```

### Recovery order on give-up

| Step | Condition | Outcome |
|---|---|---|
| 1 | `cache=` set and cache hit | return the cached value |
| 2 | `cache=` unset or cache miss, `fallback=` set | call `fallback(exc)`, return its result |
| 3 | neither path returns a value | re-raise the original exception with a [PEP 678](https://peps.python.org/pep-0678/) note |

Use `cache=` alone when stale data is acceptable. Add `fallback=` for a safety net when the cache is cold. Use `fallback=` alone for purely synthesized defaults.

## Per-call rate ceiling

If the dependency has a contractual quota (an SLA limit, a third-party rate limit), set `max_rate=` to cap the adaptive bucket's growth:

```python
--8<-- "resilience/shield_max_rate.py"
```

CUBIC will still grow the rate after recovery, but `max_rate` clamps the ceiling. Without it, the ceiling grows unbounded as the dependency stays healthy.

## Behavior on giving up

When Shield gives up (budget exhausted, attempts exhausted, or non-retryable exception) and no recovery path returns a value (see [Recovery order on give-up](#recovery-order-on-give-up)), the underlying exception is re-raised with a [PEP 678](https://peps.python.org/pep-0678/) note attached:

```python
--8<-- "resilience/shield_giveup.py"
```

The note format encodes the give-up reason (`budget exhausted`, `attempts exhausted`, `non-retryable exception`), the attempt count, the total elapsed time, and the profile name.

Callers catch the underlying exception type, unchanged. There is no `ShieldError` wrapper.

## Composing with client-side retries

Shield is the **outer** layer of resilience. Many client libraries ship their own retry logic, tuned for protocol-level transience (`Retry-After` headers, idempotency keys, modeled status codes). Shield does not replace that work. It adds a slower-timescale layer on top.

You do not need to disable the client's retries. Pass the client's terminal exception types via `timeout_errors=`:

```python
--8<-- "resilience/shield_layered.py"
```

When the inner layer exhausts its own attempts and surfaces an exception, Shield sees it. CUBIC engages only when the *outer* exception escapes, never on per-attempt blips the inner layer handled.

## What Shield does not do

- **HTTP status sniffing.** No `Retry-After` parsing, no 429/503 awareness. Call `response.raise_for_status()` inside the wrapped function and pass the resulting exception type via `timeout_errors=`.
- **Sync callables.** Async-only in 1.0. Wrap sync code in `asyncio.to_thread(...)` if needed.
- **Hedged requests.** Hedging fires N parallel attempts and returns the first success. It is a different primitive, not on the Shield roadmap.
- **Distributed retry budget.** Each `Shield` is per-process. There is no cross-replica coordination.
- **Total deadline propagation.** Shield enforces per-attempt timeouts. Wrap the whole call in `asyncio.timeout(...)` for a total deadline.

## Configuration

`Shield` follows the three-paths configuration contract.

### Programmatic

```python
--8<-- "resilience/shield_programmatic.py"
```

### Declarative

```python
--8<-- "resilience/shield_declarative.py"
```

`ApiShieldConfig`, `InternalShieldConfig`, and `SlowShieldConfig` form a discriminated union on `kind`. Each subclass freezes its profile parameters. The public fields (`timeout_errors`, `max_rate`, `cache`, `cache_key`, `fallback`) are the only knobs.

### Environmental

Prefix: `GREL_SHIELD_{NAME_UPPER}_`

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_SHIELD_{NAME_UPPER}_PROFILE` | profile | `internal` / `api` / `slow` | `api` |
| `GREL_SHIELD_{NAME_UPPER}_TIMEOUT_ERRORS` | `timeout_errors` | CSV of FQN strings (e.g. `httpx.TimeoutException,httpx.ConnectError`) | `builtins.TimeoutError` |
| `GREL_SHIELD_{NAME_UPPER}_MAX_RATE` | `max_rate` | `float` or empty | unset |

The `cache`, `cache_key`, and `fallback` arguments cannot come from env. Use the programmatic or declarative path for them.

```python
--8<-- "resilience/shield_environmental.py"
```

## Live reconfiguration

`Shield` inherits `Reconfigurable[ShieldConfig]`. Calling `shield.reconfigure(new_config)` swaps the snapshot for future calls. An in-flight `await shield.run(...)` keeps its snapshot until it completes. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Shield) for every option.
