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

### Failure-domain isolation

`uses=` scopes Providers and Components to the bulkhead, in the same shape as `Grelmicro(uses=[...])`. Inside the scope, a Pattern that resolves its *default* backend (a bare `Lock("k")`, a `cache.get(...)`, ...) picks up the bulkhead's Component instead of the app's. A Pattern with an explicit `backend=` is unaffected, so explicit choices always win. This isolates a business context (checkout, reporting) onto its own connection pool, so one context cannot exhaust another's.

```python
--8<-- "resilience/bulkhead_uses.py"
```

The bulkhead opens its `uses=` on first entry and closes them when the app shuts down, so an active `Grelmicro` app is required. Order inside `uses=` does not matter: a Provider listed after the Component that borrows it is moved ahead of it. The scope never adopts a Provider you left out, where `Grelmicro(uses=[...])` would, because it opens on the app's exit stack and would close a Provider the app still holds.

The scope belongs to one app run: whichever enters the bulkhead first. A later run in the same process opens every item again from the start. A run that overlaps the owner borrows the open items instead of opening a second set, and gives them up when the owner closes them, so give overlapping apps their own `Bulkhead` when their lifetimes differ.

Something has to open the Provider a Component borrows: list it here, list it on the app, or open it yourself. A Component that opens ahead of the Provider it borrows says which one to move. A scope that opens during startup sits below whatever the app enters after it. So a Provider the app opens later closes first, and a Component in the scope that borrows it closes against a dead client. List that Provider ahead of whatever enters the bulkhead.

Listing an item in both places is fine: whichever opens it first owns it. That holds within one app run, for the same object, a Provider, or a bare backend. It does not reach across runs: a second run that lists an item, in `uses=` or on the app itself, opens it again while the first still holds it open. Two Components you build separately around one backend are two items too, so share the Component or list the backend bare. A Component in the list claims its whole kind, so a bare Provider beside it fills only the other kinds it serves, exactly as on the app.

`micro.fake()` and `micro.override(...)` swap the app's Components, and a scope answers before the app does, so neither reaches inside `uses=`. Build the bulkhead with the backends the test wants when a test needs the scope faked.

These Components are not registered on the app, so the [backend check](../deployment.md#the-backend-check) cannot run at startup for them. It runs on first entry instead, with the same rules.

## Configuration

`Bulkhead` follows the three-paths configuration contract.

### Environmental

Prefix: `GREL_BULKHEAD_{NAME_UPPER}_`. The default instance drops the name segment and reads `GREL_BULKHEAD_*`.

--8<-- "env_gate.md"

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
