# Timeout

A **Timeout** policy bounds how long an async call may run. Use it to keep a slow dependency from blocking a request indefinitely.

**Why**

- Cap the worst-case latency of a downstream call.
- Reconfigure the deadline at runtime from a `ConfigMap` without redeploying.
- Compose with `Retry`, `CircuitBreaker`, `Bulkhead`, and `Fallback`.

`Timeout` wraps `asyncio.timeout`. When the deadline elapses, the inner block is cancelled and `TimeoutError` is raised.

## Usage

```python
--8<-- "resilience/timeout.py"
```

The policy works as an async context manager and as a decorator on async functions. Sync functions are not supported (asyncio cannot cancel sync code).

```python
--8<-- "resilience/timeout_decorator.py"
```

## Configuration

`Timeout` follows the three-paths configuration contract.

### Programmatic

```python
--8<-- "resilience/timeout_programmatic.py"
```

### Declarative

```python
--8<-- "resilience/timeout_declarative.py"
```

### Environmental

Prefix: `GREL_TIMEOUT_{NAME_UPPER}_`

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_TIMEOUT_{NAME_UPPER}_SECONDS` | `seconds` | `PositiveFloat` | required |

```python
--8<-- "resilience/timeout_environmental.py"
```

## Composition

The recommended outside-in order is **Fallback : Retry : CircuitBreaker : Bulkhead : Timeout : call**. Read more in [Composing patterns](composition.md).

```python
--8<-- "resilience/timeout_composition.py"
```

A per-attempt `Timeout` placed under `Retry` shrinks the deadline of each retry while the retry budget stays the same.

## Live reconfiguration

`Timeout` inherits `Reconfigurable[TimeoutConfig]`. Calling `policy.reconfigure(new_config)` swaps the deadline for future entries. Scopes already inside `async with` keep their original deadline. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Timeout) for every option.
