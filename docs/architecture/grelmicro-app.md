# ADR-0002: Grelmicro app object, retire module-level registries

## Status

Proposed. Targets the `0.22.0` to `0.24.0` window. Last breaking change before `1.0`.

!!! warning "Proposed API, not yet implemented"
    Every `Grelmicro`, `Sync`, `Cache`, `Tasks`, `Health`, and `Feature`
    reference in this document describes a future shape. None of these symbols
    exist in the current codebase. The shipped API today is the per-module
    `register / use_* / use` helpers. Treat snippets here as design intent,
    not runnable code.

## Context

Today every grelmicro feature owns a module-level `BackendRegistry` (`sync_backend_registry`, `cache_backend_registry`, ...). All registries auto-subscribe into a single global dict, `_ALL_REGISTRIES`. `grelmicro.lifespan()` walks that dict on entry. Primitives like `Lock("k")` resolve their backend by name through the same registry.

This pattern has three real costs:

1. **Import-time mutation.** Importing `grelmicro.cache` mutates a global. Tree-shaking (issue [#189](https://github.com/grelinfo/grelmicro/issues/189)) is hard because import order matters and side effects are silent.
2. **Test isolation requires `registry.reset()` fixtures.** Every test that touches a registry must clean up after itself or contaminate the next test.
3. **Multiple grelmicro setups in one process is impossible.** Multi-tenant servers, parallel test workers running in one event loop, and library authors composing grelmicro into their own framework all hit the same wall.

The pattern that survived in modern stacks is different. Tower (Rust), axum (Rust), Litestar (Python), and Resilience4j (Java, when used outside Spring) all converged on the same shape: an explicit container the user owns, with components registered as values, and ambient lookup via a per-task scope.

## Decision

Replace the module-level registries and `_ALL_REGISTRIES` with a `Grelmicro` class. The user constructs one, registers features on it, and opens it as an async context manager. Inside the lifespan, a `ContextVar` makes the active app available to primitives that want lazy backend lookup.

```python
from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.redis import RedisSync
from grelmicro.cache import Cache
from grelmicro.cache.redis import RedisCache
from grelmicro.task import Tasks
from grelmicro.health import Health

micro = Grelmicro()
micro.use(Sync(RedisSync("redis://primary")))
micro.use(Sync(RedisSync("redis://analytics"), name="analytics"))
micro.use(Cache(RedisCache(...)))
micro.use(Tasks())
micro.use(Health())

@micro.tasks.interval(seconds=5)
async def cleanup(): ...

@micro.health.check("redis")
async def redis_alive(): ...

async with micro:
    async with micro.sync.lock("cart"):
        ...
    async with micro.sync.lock("k", backend="analytics"):
        ...
```

### Naming

| Concept | Name | Reason |
|---|---|---|
| App class | `Grelmicro` | Matches the package spelling (`grelmicro`), same convention as `Litestar`, `Starlette`, `Pydantic`. |
| Conventional variable | `micro` | Literally the second word of the package name. Same shape as `celery = Celery(...)`. Never collides with `app = FastAPI(...)`. Carries grelmicro's microservice positioning on every line of user code. |
| Registration verb | `micro.use(feature)` | Matches Tower's `.layer(...)`, axum's `.layer(...)`, Express's `.use(...)`. Short, active voice. |
| Plugin protocol | `Feature` | Built-ins and third-parties are equal citizens. `Component` collides with UI vocab, `Plugin` implies optional, `Module` collides with Python imports. |
| Scoped override | `micro.<feature>.override(...)` | Renamed from today's `use(...)`. The new verb reads cleanly as a test or per-block substitution. |

### `Feature` protocol

```python
from types import TracebackType
from typing import Protocol, Self


class Feature(Protocol):
    """A grelmicro feature that opens with the app and exposes an API."""

    kind: str  # "sync", "cache", "tasks", "health", ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...
```

`micro.use(feature)` reads `feature.kind`, attaches the feature as `micro.<kind>`, and walks features in registration order on `async with micro:`. Reusing the same `kind` with a different instance raises. Reusing with the same instance is a no-op.

### Backend names live on the feature, not on the backend

`Sync(backend, name="analytics")` is the registration site. The backend itself stays a plain value describing connectivity. Same shape as Resilience4j's `CircuitBreakerRegistry.circuitBreaker(name, config)` and Spring's `@Qualifier("name")`.

### ContextVar for ambient lookup

`async with micro:` sets `_current_micro: ContextVar[Grelmicro]`. Primitives that resolve lazily (`Lock("k")` without an explicit `micro=` argument) call `current_micro().sync.resolve(name)` at acquire time. Outside any `async with micro:`, the resolution raises with a clear error.

The ContextVar is per asyncio task, so parallel test workers and concurrent tenants each see their own micro.

### Lifespan integration

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with micro:
        yield

app = FastAPI(lifespan=lifespan)
```

The same pattern works for FastStream, Sanic, Starlette, and bare `asyncio.run`.

## Migration

Three minor releases.

### 0.22.0

Land the new API alongside the old one. Module-level registries and `_ALL_REGISTRIES` keep working. `grelmicro.lifespan()` keeps walking them. The `Grelmicro` class and the `Feature` protocol ship as the recommended path. Documentation leads with `Grelmicro`, with the old style noted as deprecated.

Issue [#184](https://github.com/grelinfo/grelmicro/issues/184) (TaskManager registry) lands as designed for the old API. It is correct under the current pattern and converts cleanly to a `Tasks` feature later.

### 0.23.0

Module-level helpers (`grelmicro.sync.use_backend`, `grelmicro.task.use_manager`, ...) emit `DeprecationWarning`. `grelmicro.lifespan()` (free function) emits `DeprecationWarning` and forwards to a hidden default `Grelmicro` for one release.

### 0.24.0

Remove module-level helpers, `_ALL_REGISTRIES`, and the free `grelmicro.lifespan()` function. `1.0.0` ships with `Grelmicro` as the only API.

## Consequences

### What this fixes

* Import-time mutation goes away. Importing `grelmicro.cache` is a pure import.
* Tree-shake (issue [#189](https://github.com/grelinfo/grelmicro/issues/189)) becomes trivial. Apps ship only the features they construct.
* Test isolation is per-test by default. Each test makes its own `Grelmicro()`. No reset fixtures.
* Multi-tenant and library-author composition both work. Two `Grelmicro` instances in one process are independent.

### What it costs

* Every snippet changes. Estimated 30 files in `docs/snippets/`.
* `Lock("k")` as a free name still works inside a micro context, but constructing a primitive outside any micro becomes an error. Today it would silently resolve a global.
* One extra concept (`Feature`) to learn. Offset by removing `BackendRegistry`, `_ALL_REGISTRIES`, and per-module `register / unregister / use_*` helpers.
* The decision about `ContextVar` lookup vs. explicit `micro=` argument on every primitive is left open. The default is `ContextVar` for ergonomics. Users who want fully explicit wiring pass `micro=` and never rely on ambient state.

### What stays the same

* Backends are unchanged. `RedisSync`, `RedisCache`, etc. remain plain async context managers.
* Reconfigure semantics are unchanged. `primitive.reconfigure(new_config)` is per-instance.
* The sync-from-thread bridge is unchanged.

## Open questions

1. **`micro.<feature>.override(...)` vs. `micro.override(feature=...)`.** Per-feature `override` is closer to today's `grelmicro.sync.use(...)`. App-level `override(sync=..., cache=...)` reads better for tests that stub multiple features at once. Decide before 0.22.0.
2. **Does `micro.use(Sync(...))` return `Self` for chaining or the feature?** Returning the feature lets the user keep a reference for `reconfigure(...)` calls. Returning `Self` enables fluent construction. Pick one.
3. **Should `Grelmicro` accept features in the constructor?** `Grelmicro(Sync(...), Cache(...))` as sugar over `micro.use(...)`. Optional. Decide on style ground.
4. **Lifespan order on conflicting features.** Today registries run in import order. With explicit `use(...)`, registration order is the order. Document this and make it deterministic.

## References

* Tower (Rust), `tower::ServiceBuilder`: <https://docs.rs/tower/latest/tower/struct.ServiceBuilder.html>
* axum (Rust), `Router::with_state`: <https://docs.rs/axum/latest/axum/struct.Router.html#method.with_state>
* Litestar (Python), plugins: <https://docs.litestar.dev/latest/usage/plugins/index.html>
* Resilience4j (Java), registries: <https://resilience4j.readme.io/docs/circuitbreaker#create-and-configure-a-circuitbreaker>
* OpenTelemetry context propagation, `ContextVar` per task: <https://opentelemetry.io/docs/specs/otel/context/>
* Express.js, `app.use(middleware)`: <https://expressjs.com/en/api.html#app.use>
