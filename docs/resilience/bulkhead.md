# Bulkhead

A **Bulkhead** caps how many calls run at once. Rate limiting bounds requests per unit time. A bulkhead bounds *concurrent in-flight* work, so one slow dependency cannot consume every worker and starve the rest of the app.

**Why**

- Bound concurrent in-flight business operations.
- Fail fast when saturated instead of queueing unboundedly.
- Keep blocking work on a dedicated thread pool, off the event loop and off the shared executor.

## Usage

`Bulkhead` works as an async context manager and as a decorator on async functions. When the limit is reached, a caller waits up to `max_wait` seconds for a permit, then is rejected with `BulkheadFullError`.

```python
--8<-- "resilience/bulkhead.py"
```

The default fails fast: with no `max_wait`, a full bulkhead rejects immediately. Set `max_wait` to let callers queue briefly for a permit.

### Bounded blocking work

`to_thread` runs a blocking function on the bulkhead's own thread pool when `max_workers` is set, otherwise on the event loop's shared executor.

```python
--8<-- "resilience/bulkhead_to_thread.py"
```

## Configuration

`Bulkhead` follows the three-paths configuration contract.

### Environmental

Prefix: `GREL_BULKHEAD_{NAME_UPPER}_`

| Env var | Field | Type | Default |
|---|---|---|---|
| `GREL_BULKHEAD_{NAME_UPPER}_MAX_CONCURRENT` | `max_concurrent` | `PositiveInt` | unbounded |
| `GREL_BULKHEAD_{NAME_UPPER}_MAX_WAIT` | `max_wait` | `NonNegativeFloat` | fail fast |
| `GREL_BULKHEAD_{NAME_UPPER}_MAX_WORKERS` | `max_workers` | `PositiveInt` | shared executor |

## Composition

The recommended outside-in order is **Fallback → Retry → CircuitBreaker → Bulkhead → Timeout → call**. Read more in [Composing patterns](composition.md). Placing the bulkhead above the timeout caps concurrency before a call enters its timeout window.

## Live reconfiguration

`Bulkhead` inherits `Reconfigurable[BulkheadConfig]`. Calling `bulkhead.reconfigure(new_config)` applies a new `max_concurrent` to calls admitted after the swap. Calls already inside keep their permit. Changing `max_workers` rebuilds the private thread pool. See [Live reconfiguration](../architecture/reconfigure.md).

## Reference

See the [API reference](../reference/resilience.md#grelmicro.resilience.Bulkhead) for every option.
