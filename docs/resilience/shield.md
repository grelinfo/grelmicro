# Shield

A **Shield** is the outer layer of resilience around one async call to one dependency. It bundles a per-attempt timeout, exponential-jittered retry gated by a consecutive-failure budget, and a CUBIC-style adaptive rate limiter that engages only after sustained slow-downs. One decorator, one knob.

**Why**

- Wrap one call to one dependency without composing four primitives by hand.
- Survive transient slow-downs (network blips, brief overloads) without changing the call site.
- Pace the whole client down automatically when a dependency keeps timing out, then ramp back gradually.
- Stay invisible on the happy path. The adaptive layer enables itself only after the first slow-down.

For background on the algorithm (CUBIC tuning, retry-budget math, the timeout estimator), see [`Shield` algorithm spec](../architecture/shield-spec.md).

## Usage

```python
import httpx
from grelmicro.resilience import shield


@shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content
```

Pick a profile factory (`shield.internal`, `shield.api`, `shield.slow`), tell Shield which exceptions mean "the dependency is slow", done. Everything else propagates unchanged.

The zero-argument form uses the `api` profile with the default `timeout_errors=(TimeoutError,)`:

```python
@shield
async def cheap_call() -> None:
    async with asyncio.timeout(5):
        await do_work()
```

Shield is **async-only**. Use it on coroutine functions.

## Choose a profile

| Profile | Use when | Initial rate (per second) | Initial timeout |
|---|---|---|---|
| `internal` | In-cluster RPC. Healthy services, fast latency, tight budgets. | `100` | `1.0s` |
| `api` (default) | External HTTP APIs. Moderate latency, occasional outages, third-party SLAs. | `2` | `10.0s` |
| `slow` | Long-running calls: LLM inference, batch jobs, large queries. | `0.5` | `120.0s` |

Three profiles cover the common cases. They are mutually exclusive: pick one per `Shield` instance. To pace differently, switch profile, do not tune individual fields. The internal parameters (retry budget capacity, CUBIC curve constants, backoff scale, timeout clamps) are fixed by profile choice. See [profile defaults](../architecture/shield-spec.md#built-in-profiles) for the full table.

```python
@shield.internal(timeout_errors=(MyRpcTimeout,))
async def call_internal_rpc(): ...

@shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def call_external_api(): ...

@shield.slow(timeout_errors=(MyLLMError,))
async def call_llm(prompt: str): ...
```

If the three profiles do not fit, you have outgrown Shield. Compose `Retry`, `Timeout`, `RateLimiter` and `CircuitBreaker` directly instead.

## Exception classification

Shield classifies the wrapped call's outcome by exception type only. There is no response envelope, no status code, no `Retry-After` header.

| Raised by the wrapped call | Retried? | Triggers CUBIC shrink? | Consumes retry budget? |
|---|---|---|---|
| Any type in `timeout_errors` (or its subclasses) | yes | yes | yes |
| Any other `Exception` subclass | no, propagates immediately | no | no |
| Subclass of `ResilienceError` | no, propagates immediately | no | no |
| `BaseException` outside `Exception` ([PEP 654](https://peps.python.org/pep-0654/) groups, `KeyboardInterrupt`, `CancelledError`, `SystemExit`) | no, propagates immediately | no | no |

You declare what "transient" means by passing exception types to `timeout_errors=`. Anything else surfaces unchanged.

The effective tuple is always `user_tuple + (TimeoutError,)`. Shield's own per-attempt timeout raises `TimeoutError`, and the library guarantees that signal is always retryable, regardless of what the user passed.

The default when the argument is omitted is `(TimeoutError,)`, which covers the per-attempt timeout and any standard Python timeout the wrapped callable raises.

## Sharing across functions

Build a `Shield` instance once and decorate multiple functions with it. They share one retry budget and one CUBIC controller, which is the correct topology when several functions hit the same dependency.

```python
from grelmicro.resilience import Shield

github = Shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
)


@github
async def list_repos(): ...


@github
async def get_repo(): ...
```

One `Shield` instance per logical dependency. Two functions hitting GitHub share one budget. Two functions hitting GitHub and Stripe get two `Shield` instances.

The `name=` argument is the registration name used in logs, metrics, and the [PEP 678](https://peps.python.org/pep-0678/) notes attached on give-up. It defaults to the wrapped function's `__qualname__` when used as `@shield.api(...)`, and is required positional when used as `Shield.api(...)`.

## Imperative form

For inline calls that span multiple statements, use `Shield.run`:

```python
github = Shield.api("github", timeout_errors=(httpx.TimeoutException,))

async def handler():
    response = await github.run(client.get, url)
    body = await github.run(parse_response, response)
    return body
```

`Shield.run(fn, *args, **kwargs)` calls `fn(*args, **kwargs)` under the same retry-budget and adaptive-bucket state as the decorator form.

## Cache and fallback

On give-up (retry budget exhausted, attempts cap reached, or non-retryable exception), Shield tries two recovery paths in order: a cache lookup, then a fallback callable. Either or both can be set.

### Cached fallback

Pass a `Cache` instance to `cache=`. Shield writes the return value on every success and reads it on give-up:

```python
from grelmicro.cache import TTLCache
from grelmicro.resilience import shield

@shield.api(
    "prices",
    timeout_errors=(httpx.TimeoutException,),
    cache=TTLCache(ttl=300),
)
async def fetch_price(symbol: str) -> Decimal: ...
```

Behavior:

- **On success**: `await cache.set(key, return_value)` runs fire-and-forget. A cache write failure is logged at debug and never propagates.
- **On give-up**: `value = await cache.get(key)` runs. A cache hit returns the cached value. A cache miss continues to the next recovery path.
- **Key**: `f"{shield.name}:{stable_hash(args, kwargs)}"` by default. Override with `cache_key=` for control over what the key looks like:

```python
@shield.api(
    "prices",
    timeout_errors=(httpx.TimeoutException,),
    cache=TTLCache(ttl=300),
    cache_key=lambda symbol, *_: f"price:{symbol}",
)
async def fetch_price(symbol: str) -> Decimal: ...
```

Non-hashable arguments (Pydantic models, dataclasses, etc.) are hashed via stable `repr()`. Override `cache_key=` for non-default behavior.

### Custom fallback

Pass a callable to `fallback=` for the case where the cache misses (or no cache is set). The callable receives the exception that escaped Shield:

```python
async def last_known_price(exc: Exception) -> Decimal:
    return Decimal("0")


@shield.api(
    "prices",
    timeout_errors=(httpx.TimeoutException,),
    cache=TTLCache(ttl=300),
    fallback=last_known_price,
)
async def fetch_price(symbol: str) -> Decimal: ...
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
@shield.api(
    "stripe",
    timeout_errors=(stripe.error.APIConnectionError,),
    max_rate=10.0,
)
async def charge_card(...): ...
```

CUBIC will still grow the rate after recovery, but `max_rate` clamps the ceiling. Without it, the ceiling grows unbounded as the dependency stays healthy.

## Behavior on giving up

When Shield gives up (budget exhausted, attempts exhausted, or non-retryable exception) and no recovery path returns a value (see [Recovery order on give-up](#recovery-order-on-give-up)), the underlying exception is re-raised with a [PEP 678](https://peps.python.org/pep-0678/) note attached:

```python
try:
    await fetch(url)
except httpx.TimeoutException as exc:
    print(exc.__notes__)
    # ['shield: budget exhausted after 4/4 attempts in 18.30s (api profile)']
```

The note format encodes the give-up reason (`budget exhausted`, `attempts exhausted`, `non-retryable exception`), the attempt count, the total elapsed time, and the profile name.

Callers catch the underlying exception type, unchanged. There is no `ShieldError` wrapper.

## Composing with client-side retries

Shield is the **outer** layer of resilience. Many client libraries ship their own retry logic, tuned for protocol-level transience (`Retry-After` headers, idempotency keys, modeled status codes). Shield does not replace that work. It adds a slower-timescale layer on top.

You do not need to disable the client's retries. Pass the client's terminal exception types via `timeout_errors=`:

```python
@shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
)
async def fetch(url: str) -> httpx.Response:
    return await retrying_httpx_client.get(url)
```

When the inner layer exhausts its own attempts and surfaces an exception, Shield sees it. CUBIC engages only when the *outer* exception escapes, never on per-attempt blips the inner layer handled. See the [layering note in the spec](../architecture/shield-spec.md#composing-with-client-side-retries-layered-resilience) for the full reasoning.

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
from grelmicro.resilience import Shield

github = Shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
    max_rate=20.0,
)
```

### Declarative

```python
from grelmicro.resilience import ApiShieldConfig, Shield

config = ApiShieldConfig(
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
    max_rate=20.0,
)
github = Shield("github", config=config)
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
github = Shield("github", env_load=True)
```

## Live reconfiguration

`Shield` inherits `Reconfigurable[ShieldConfig]`. Calling `shield.reconfigure(new_config)` swaps the snapshot for future calls. An in-flight `await shield.run(...)` keeps its snapshot until it completes. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Shield) for every option.
