# Live reconfiguration

This page is the engineering side of `Component.reconfigure(new_config)`. It documents the contract behind the [`Reconfigurable`][grelmicro._config.Reconfigurable] mixin and explains the choices a contributor needs to know before adding `reconfigure` support to a new component.

## The contract

`reconfigure(new_config)` swaps a live component's configuration without rebuilding the component. Runtime state on the backend (token counts, lease handles, in-flight calls) is preserved across the swap.

Every call enforces:

| Invariant | Behavior |
|---|---|
| Type match | `type(new_config) is type(self._config)` or `TypeError` |
| Equality short-circuit | `new_config == self._config` returns immediately |
| Writer serialization | `anyio.Lock` held for the rebuild |
| Atomic publish | `self._config = new_config` runs after `_apply_reconfigure` |
| Failure rollback | If `_apply_reconfigure` raises, `self._config` is untouched |

In-flight operations on the previous config complete on the previous strategy and previous fallback. Operations started after `reconfigure` returns see the new values.

## How the mixin works

The mixin is small enough to read in one screen:

```python
async def reconfigure(self, new_config: ConfigT) -> None:
    current = self._config
    if type(new_config) is not type(current):
        raise TypeError(...)
    if new_config == current:
        return
    async with self._reconfigure_lock:
        if new_config == self._config:
            return
        await self._apply_reconfigure(new_config)
        self._config = new_config
```

The two equality checks are not redundant. The first runs on the hot path so the common no-op case never blocks on the lock. The second runs under the lock so two callers racing to set the same value do not both trigger a rebuild.

The mixin commits `self._config` after `_apply_reconfigure` returns. This means subclasses cannot forget to assign last, and a raise inside `_apply_reconfigure` always preserves the previous config.

## Reader safety

A reconfigurable component publishes a single immutable read-side snapshot. Each operation captures that snapshot with one attribute read at the top:

```python
state = self._state
config = state.config
strategy = state.strategy
```

Single-attribute reads of a Python object reference are atomic under the GIL and remain atomic on free-threaded 3.13+, so no read-side lock is required. Every config-dependent decision in the operation derives from `state`, never from `self._config` or any other instance attribute that could swap independently.

Subclasses MUST bundle every cached derived value (config, strategy, fallback, limits) into one frozen snapshot type and publish it with a single assignment in `_apply_reconfigure`. Caching a derived value on a separate attribute reintroduces a multi-attribute window: a reader could observe the new derived value with the previous snapshot, applying mismatched parameters in one call.

`RateLimiter` follows this rule: its `_State` dataclass holds both `config` and the bound `strategy`, and every hot path captures `state = self._state` once.

## Implementing `_apply_reconfigure`

Components that read config fields directly per-call inherit the no-op default and need no override:

```python
class Lock(Reconfigurable[LockConfig]):
    def __init__(self, name: str, ...) -> None:
        ...
        self._config = config
        self._reconfigure_lock = anyio.Lock()
```

Components that cache derived state override `_apply_reconfigure` and rebuild those caches into instance attributes. The mixin commits `self._config` for them:

```python
class RateLimiter(Reconfigurable[RateLimiterConfig]):
    async def _apply_reconfigure(self, new_config):
        new_strategy = self.backend.bind(new_config)
        self._state = _State(config=new_config, strategy=new_strategy)
```

Build new derived values into locals first, then publish them all in one frozen-snapshot assignment. If any step raises (for example `backend.bind`), the snapshot has not been mutated and the previous state is preserved exactly.

## Out of scope

The library does not ship file watchers, signal handlers, or ConfigMap pollers. Wiring `reconfigure` to a SIGHUP handler or a Kubernetes informer is application-level work. See [Configuration](../config.md) for one worked example.

Hot-swapping the backend from the new config is also out of scope. `_apply_reconfigure` does not read the backend identity from `new_config`. The component continues to resolve its backend the same way it did before reconfigure: a backend instance passed at construction is reused as-is, while a backend resolved through the registry is re-resolved on each call so that task-scoped overrides keep working. `reconfigure` accepts a new config of the same runtime type only, not a different config subclass.

## Related

- [Configuration](../config.md): the three paths and the resolution order that produce a config in the first place.
- [Configuration internals](config.md): the engineering side of `resolve_config` and the `Config` contract.
