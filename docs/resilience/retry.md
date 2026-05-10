# Retry

A **Retry** policy repeats a failing call with backoff and jitter. Use it for transient errors that are safe to retry: network blips, brief overloads, deadline-related failures.

**Why**

- Survive flaky network paths without changing the call site.
- Apply exponential backoff with jitter so retries do not stampede a recovering dependency.
- Stay safe by default: retries trigger only on the exception classes you allow.

## Usage

```python
--8<-- "resilience/retry.py"
```

The decorator works on async and sync functions. It auto-detects which kind it wraps.

For inline retries that span multiple statements, use the block form:

```python
--8<-- "resilience/retry_block.py"
```

`when=` is required. There is no default. Pass a [`Match`](#filtering-outcomes-with-match) instance, or one of the shorthand forms (an exception class, a tuple of classes, or a predicate callable). See the next section for the full filter DSL.

## Filtering outcomes with `Match`

`Match` is the DSL every resilience strategy uses to decide whether an outcome (an exception OR a return value) should engage the strategy. The `when=` parameter on `Retry` accepts any `Match` instance, plus the bare-class shorthand for the simple case.

### Exception filtering

```python
from grelmicro.resilience import Match, Retry

Retry("api", when=Match.exception(httpx.HTTPError))
Retry("api", when=Match.exception(httpx.HTTPError, OSError))
Retry("api", when=Match.exception(lambda e: e.status >= 500))
Retry("api", when=Match.exception_message(contains="timeout"))
Retry("api", when=Match.exception_message(regex=r"^5\d\d "))
Retry("api", when=Match.exception_cause(KeyError))
```

### Result filtering

```python
Retry("polling", when=Match.result(None))
Retry("polling", when=Match.result(False))
Retry("polling", when=Match.result(lambda r: r.status_code >= 500))
```

`Match.result(callable)` always treats the argument as a predicate. To match a function literal exactly, wrap with `lambda r: r is my_fn`.

### Composition

```python
# OR
Retry("api", when=Match.exception(httpx.HTTPError) | Match.result(None))

# AND
Retry("api", when=Match.exception(httpx.HTTPError) & Match.exception(lambda e: e.status >= 500))

# NOT (one symmetric `not_*` per primitive)
Retry("api", when=Match.not_exception(ValueError))
Retry("api", when=Match.not_result(None))
Retry("api", when=Match.not_exception_message(contains="ok"))
Retry("api", when=Match.not_exception_cause(KeyError))
```

Use `|` for OR and `&` for AND. Each primitive (`exception`, `result`, `exception_message`, `exception_cause`) has a `not_*` twin for the negated form.

### Worked example

```python
--8<-- "resilience/retry_match.py"
```

### What never retries

`asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit` are `BaseException` subclasses outside `Exception`. They always propagate, regardless of the `Match` you pass. This is required for correct asyncio shutdown.

## Backoff algorithms

`Retry` ships five algorithms. The default is exponential with full jitter. Pick by purpose.

| Algorithm | Use when |
|---|---|
| `ExponentialBackoff` (default) | Network and HTTP retries. Doubling delay with jitter avoids retry storms. |
| `ConstantBackoff` | Polling-style retries (waiting for a job). Fixed interval is predictable. |
| `LinearBackoff` | Steady, predictable growth without the early-attempt cluster of exponential. |
| `FibonacciBackoff` | Smoother growth than exponential, faster than linear. |
| `RandomBackoff` | Uniform random delay in a fixed range. Maximum spread, no growth. |

The factory classmethods build the right config for you:

```python
policy = Retry.exponential("payments", when=httpx.HTTPError, attempts=5)
polling = Retry.constant("wait_job", when=NotReady, attempts=20, delay=1.0)
```

### Exponential

The raw wait before retry `N` is `min(base_delay * 2 ** (N - 1), max_delay)`. It doubles each attempt until it reaches the cap. With the defaults (`base_delay=0.1`, `max_delay=30.0`), the raw wait is `0.1s`, `0.2s`, `0.4s`, `0.8s`, `1.6s`, ..., capped at `30.0s`.

Jitter then transforms each raw wait into the actual sleep (see [Jitter](#jitter) below). The actual sleep may be smaller than the raw value.

#### Jitter

**Jitter** is randomness added to the wait so concurrent clients do not retry at the same instant and overwhelm the recovering server.

Pick a mode by how much spread you want. Default: `full`.

| Jitter | Spread | When to use |
|---|---|---|
| `none` | none | Single client. Never with concurrency. |
| `full` (default) | maximum | The safe default. |
| `equal` | half | When you need predictable timing. |
| `decorrelated` | adaptive | High contention on a shared dependency. |

### Constant

A fixed delay between retries. One field: `delay` (seconds, default `1.0`).

## Configuration

`Retry` follows the three-paths configuration contract.

### Programmatic

```python
--8<-- "resilience/retry_programmatic.py"
```

### Declarative

```python
--8<-- "resilience/retry_declarative.py"
```

### Environmental

Prefix: `GREL_RETRY_{NAME_UPPER}_`

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_RETRY_{NAME_UPPER}_ATTEMPTS` | `attempts` | `int` (>= 1) | `3` |
| `GREL_RETRY_{NAME_UPPER}_WHEN` | `when` | CSV or JSON list of FQN strings (e.g. `httpx.HTTPError`). Coerced to `Match.exception(...)`. Predicate forms cannot come from env. | required |
| `GREL_RETRY_{NAME_UPPER}_BACKOFF` | `backoff` | JSON object with a `type` field (see below) | `{"type":"exponential"}` |

The full backoff config is a discriminated Pydantic union, so the env value is parsed as one JSON object. Each algorithm accepts the same fields it takes in code:

| `type` | Fields |
|---|---|
| `exponential` | `base_delay`, `max_delay`, `jitter` (`none` / `full` / `equal` / `decorrelated`) |
| `constant` | `delay` |
| `linear` | `base_delay`, `max_delay` |
| `fibonacci` | `base_delay`, `max_delay` |
| `random` | `min_delay`, `max_delay` |

```python
--8<-- "resilience/retry_environmental.py"
```

The callable form of `on` cannot come from env. Use the FQN list for env-driven configs.

## Composition with Circuit Breaker

Retry and Circuit Breaker compose by intent. When the breaker is `OPEN`, it raises `CircuitBreakerError`. Pick a narrow `when=` allowlist so the retry loop does not swallow that signal:

```python
--8<-- "resilience/retry_composition.py"
```

A broad allowlist (`when=Exception`) would retry through the open breaker. The narrow allowlist lets the breaker do its job.

## Behavior on exhaustion

When `attempts` is exhausted, the underlying exception is re-raised with a [PEP 678](https://peps.python.org/pep-0678/) note attached:

```python
try:
    await fetch(url)
except httpx.ConnectError as exc:
    print(exc.__notes__)
    # ['retry: 3/3 attempts exhausted in 1.40s (exponential backoff)']
```

Callers catch the underlying error type, unchanged. There is no `RetryError` wrapper class.

## Live reconfiguration

`Retry` inherits `Reconfigurable[RetryConfig]`. Calling `policy.reconfigure(new_config)` swaps the snapshot for future loops. An in-flight `async for attempt in policy:` keeps its snapshot until it completes. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Retry) for every option.
