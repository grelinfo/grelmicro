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

`on=` is required. There is no default. Pass an exception class, a tuple of classes, or a predicate callable.

## Backoff algorithms

`Retry` ships two algorithms: exponential (default) and constant. Pick by purpose.

| Algorithm | Use when |
|---|---|
| `ExponentialBackoffConfig` | Network and HTTP retries. Doubling delay with jitter avoids retry storms. |
| `ConstantBackoffConfig` | Polling-style retries (waiting for a job). Fixed interval is predictable. |

The factory classmethods build the right config for you:

```python
policy = Retry.exponential("payments", on=httpx.HTTPError, attempts=5)
polling = Retry.constant("wait_job", on=NotReady, attempts=20, delay=1.0)
```

### Exponential

Delay before retry `N` is `min(base_delay * 2 ** (N - 1), max_delay)`, then jittered. The defaults follow the AWS recipe: `base_delay=0.1`, `max_delay=30.0`, `jitter="full"`.

| Jitter | Behavior |
|---|---|
| `none` | Use the raw computed delay. |
| `full` | Sample uniformly from `[0, raw]`. AWS recipe. |
| `decorrelated` | Chain samples across attempts. Use under high contention. |

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
| `GREL_RETRY_{NAME_UPPER}_ON` | `on` | CSV or JSON list of FQN strings (e.g. `httpx.HTTPError`) | required |
| `GREL_RETRY_{NAME_UPPER}_BACKOFF` | `backoff.type` | `exponential` or `constant` | `exponential` |
| `GREL_RETRY_{NAME_UPPER}_BASE_DELAY` | `backoff.base_delay` (exponential only) | `float` (> 0) | `0.1` |
| `GREL_RETRY_{NAME_UPPER}_MAX_DELAY` | `backoff.max_delay` (exponential only) | `float` (> 0) | `30.0` |
| `GREL_RETRY_{NAME_UPPER}_JITTER` | `backoff.jitter` (exponential only) | `none` / `full` / `decorrelated` | `full` |
| `GREL_RETRY_{NAME_UPPER}_DELAY` | `backoff.delay` (constant only) | `float` (> 0) | `1.0` |

```python
--8<-- "resilience/retry_environmental.py"
```

The callable form of `on` cannot come from env. Use the FQN list for env-driven configs.

## Composition with Circuit Breaker

Retry and Circuit Breaker compose by intent. When the breaker is `OPEN`, it raises `CircuitBreakerError`. Pick a narrow `on=` allowlist so the retry loop does not swallow that signal:

```python
--8<-- "resilience/retry_composition.py"
```

A broad allowlist (`on=Exception`) would retry through the open breaker. The narrow allowlist lets the breaker do its job.

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
