# The `Grelmicro` class

!!! warning "Proposed API, not yet implemented"
    Every `Grelmicro`, `Sync`, `Cache`, `Tasks`, `Health`, and `Feature`
    reference below describes the target shape. None of these symbols
    exist yet. The shipped API today is the per-module
    `register / use_* / use` helpers.

The `Grelmicro` class is the single entry point of the library. The user constructs one, registers features on it, and opens it as an async context manager. There are no module-level registries, no global state, no import-time mutation.

## Shape

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

async with micro:
    async with micro.sync.lock("cart"):
        ...
    async with micro.sync.lock("k", backend="analytics"):
        ...
```

## Why an explicit container

Module-level registries have three structural costs:

1. **Import-time mutation.** Importing `grelmicro.cache` mutates a global. Tree-shaking is hard because import order matters and side effects are silent.
2. **Test isolation requires reset fixtures.** Every test that touches a registry must clean up or contaminate the next.
3. **Multiple grelmicro setups in one process is impossible.** Multi-tenant servers, parallel test workers, and library authors composing grelmicro into their own framework all hit the same wall.

Tower (Rust), axum (Rust), Litestar (Python), and Resilience4j (Java) all converged on the same answer: an explicit container the user owns, components registered as values, ambient lookup via a per-task scope.

## Naming

| Concept | Name | Reason |
|---|---|---|
| App class | `Grelmicro` | Matches the package spelling, same convention as `Litestar`, `Starlette`, `Pydantic`. |
| Conventional variable | `micro` | Second word of the package. Same shape as `celery = Celery(...)`. Never collides with `app = FastAPI(...)`. |
| Registration verb | `micro.use(feature)` | Matches Tower, axum, Express. Short, active. |
| Plugin protocol | `Feature` | Built-ins and third-parties are equal citizens. |
| Scoped override | `micro.<feature>.override(...)` | Reads cleanly as a test or per-block substitution. |

## `Feature` protocol

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

## Backend names live on the feature

`Sync(backend, name="analytics")` is the registration site. The backend itself stays a plain value describing connectivity. Same shape as Resilience4j's `CircuitBreakerRegistry.circuitBreaker(name, config)` and Spring's `@Qualifier("name")`.

## ContextVar for ambient lookup

`async with micro:` sets `_current_micro: ContextVar[Grelmicro]`. Primitives that resolve lazily (`Lock("k")` without an explicit `micro=`) call `current_micro().sync.resolve(name)` at acquire time. Outside any `async with micro:`, the resolution raises with a clear error.

The ContextVar is per asyncio task, so parallel test workers and concurrent tenants each see their own micro.

## Lifespan integration

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

## What is unchanged

* Backends (`RedisSync`, `RedisCache`, ...) remain plain async context managers.
* Reconfigure semantics are unchanged. `primitive.reconfigure(new_config)` is per-instance.
* The sync-from-thread bridge is unchanged.

## Tracking

Implementation, migration, and open questions are tracked as GitHub issues under the [`area:core`](https://github.com/grelinfo/grelmicro/issues?q=is%3Aissue+label%3Aarea%3Acore) label.

## References

* Tower (Rust), `tower::ServiceBuilder`: <https://docs.rs/tower/latest/tower/struct.ServiceBuilder.html>
* axum (Rust), `Router::with_state`: <https://docs.rs/axum/latest/axum/struct.Router.html#method.with_state>
* Litestar (Python), plugins: <https://docs.litestar.dev/latest/usage/plugins/index.html>
* Resilience4j (Java), registries: <https://resilience4j.readme.io/docs/circuitbreaker#create-and-configure-a-circuitbreaker>
* OpenTelemetry context propagation: <https://opentelemetry.io/docs/specs/otel/context/>
* Express.js, `app.use(middleware)`: <https://expressjs.com/en/api.html#app.use>
