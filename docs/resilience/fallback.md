# Fallback

A **Fallback** policy returns a safe value when a call fails. Use it to degrade gracefully: "if the live recommendation service fails, return cached recommendations or an empty list".

**Why**

- Keep the user-facing path working when a dependency is down.
- Replace a hard error with a quiet default, without changing every call site.
- Compose with Retry and Circuit Breaker so the fallback only fires after retries are exhausted or the breaker is open.

## Usage

```python
--8<-- "resilience/fallback.py"
```

The decorator works on async and sync functions. It auto-detects which kind it wraps.

Use `factory=` instead of `default=` when the fallback value should be computed from the exception or read from a cache:

```python
--8<-- "resilience/fallback_factory.py"
```

`default=` and `factory=` are mutually exclusive. Exactly one must be set.

For inline fallback that spans multiple statements, use the block form:

```python
--8<-- "resilience/fallback_block.py"
```

The block yields a `FallbackResult`. Call `result.set(value)` on success. If a matching exception is raised inside the block, it is suppressed and `result.value` is the configured default or the factory output.

## Filtering exceptions

`when=` accepts the same shorthand as `Retry`'s `when=`: an exception class, a tuple of classes, a predicate, or a `Match` instance.

```python
from grelmicro.resilience import Fallback, Match

Fallback("api", when=httpx.HTTPError, default=None)
Fallback("api", when=(httpx.HTTPError, OSError), default=None)
Fallback("api", when=lambda e: e.status >= 500, default=None)
Fallback("api", when=Match.exception(httpx.HTTPError), default=None)
```

See [Filtering outcomes with `Match`](retry.md#filtering-outcomes-with-match) for the full DSL. Only exception-shaped matchers make sense for `Fallback`: result matching does not apply.

### What never falls back

`asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit` are `BaseException` subclasses outside `Exception`. They always propagate, regardless of `when=`. This is required for correct asyncio shutdown.

## Configuration

Build the policy with keyword arguments. Set `when` to choose which exceptions fall back, and `default` or `factory` for the safe value.

```python
--8<-- "resilience/fallback_programmatic.py"
```

### Environment variables

Prefix: `GREL_FALLBACK_{NAME_UPPER}_`. The default instance drops the name segment and reads `GREL_FALLBACK_*`.

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_FALLBACK_{NAME_UPPER}_WHEN` | `when` | CSV or JSON list of FQN strings (e.g. `httpx.HTTPError`). Coerced to `Match.exception(...)`. Predicate forms cannot come from env. | required |
| `GREL_FALLBACK_{NAME_UPPER}_DEFAULT` | `default` | JSON value (`[]`, `null`, `42`, ...). Strings that fail to parse are kept verbatim (`hello` stays `"hello"`). Mutually exclusive with `factory=`. | mutually exclusive with factory |

The JSON parse runs only on env values. A `default="[1,2,3]"` passed in code stays a string. The `factory` callable cannot come from env. Use `default` for env-driven configs, or build the `FallbackConfig` in code.

```python
--8<-- "resilience/fallback_environmental.py"
```

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition, see [Declarative configuration](../advanced/config.md).

## Composition

The recommended outside-in order is **Fallback → Retry → CircuitBreaker → Bulkhead → Timeout → call**. Read more in [Composing patterns](composition.md).

```python
--8<-- "resilience/fallback_composition.py"
```

## Live reconfiguration

`Fallback` inherits `Reconfigurable[FallbackConfig]`. Calling `policy.reconfigure(new_config)` swaps the snapshot for future calls. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Fallback) for every option.
