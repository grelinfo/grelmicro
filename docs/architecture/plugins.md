# Plugins

grelmicro discovers Providers and Adapters through entry-point groups. A
third-party package registers under these groups and resolves by short name,
so grelmicro never has to depend on the vendor. First-party Providers and
Adapters use the very same path: there is no special case.

## The three groups

| Group | Maps | Example |
|---|---|---|
| `grelmicro.providers` | a vendor short name to a `Provider` class | `redis = "grelmicro.providers.redis:RedisProvider"` |
| `grelmicro.{kind}.adapters` | a short name to an Adapter class for one component kind | `redis = "grelmicro.coordination.redis:RedisLockAdapter"` |
| `grelmicro.integrations` | a web framework's top-level module name to its integration module | `fastapi = "grelmicro.integrations.fastapi"` |

A Provider covers the vendor axis: one Provider per vendor. An Adapter covers
the algorithm axis within a kind, so several adapters can share one Provider
(a Redis lock and a Redis cache both run on `RedisProvider`).

The component kinds are `coordination`, `coordination.election`, `coordination.schedule`, `cache`,
`ratelimiter`, and `circuitbreaker`.

## Publish a third-party integration

`micro.install(app)` resolves the framework through `grelmicro.integrations`.
The key is the framework's top-level module name, and the lookup walks the app
class's MRO, so a `FastAPI` subclass declared in your own package still
matches on `fastapi`. Only the matching module is imported, so `install` never
loads a framework the app does not use.

An integration module exposes two functions:

```python
def install(app, micro, *, ambient: bool = True) -> None: ...
def is_bound(app) -> bool: ...
```

`install` opens `micro` alongside the framework's own lifecycle and adds the
per-handler binding. `is_bound` reports whether that binding is present, which
is what `micro.check_ambient_binding(app)` and `micro.describe(app)` read to
catch a forgotten `install`.

Declare it the same way as a Provider:

```toml
[project.entry-points."grelmicro.integrations"]
sanic = "grelmicro_sanic:integration"
```

## Publish a third-party adapter

Say you ship `grelmicro-mongo` with a Mongo-backed lock. Write the Provider
and the Adapter, then declare them in your package's `pyproject.toml`:

```toml
[project.entry-points."grelmicro.providers"]
mongo = "grelmicro_mongo:MongoProvider"

[project.entry-points."grelmicro.coordination.adapters"]
mongo = "grelmicro_mongo:MongoLockAdapter"
```

Once your package is installed alongside grelmicro, the name `mongo` resolves
through the same loader grelmicro uses for its own backends. Users wire it up
exactly like a first-party backend:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro_mongo import MongoProvider

mongo = MongoProvider("mongodb://localhost:27017")
micro = Grelmicro(uses=[Coordination(mongo)])
```

A worked skeleton lives in
[`examples/third-party-adapter/`](https://github.com/grelinfo/grelmicro/tree/main/examples/third-party-adapter).

### Capture the event loop

Lock, schedule, cache, and circuit-breaker backends must capture the running
loop on `__aenter__` and keep it in a `_loop` attribute:

```python
async def __aenter__(self) -> Self:
    self._loop = asyncio.get_running_loop()
    return self
```

The sync adapters (`Lock.from_thread`, `TaskLock.from_thread`, the sync
`@cached` wrapper, `CircuitBreaker.from_thread`) dispatch coroutines back into
that loop from a worker thread. The protocols declare `_loop`, so a type
checker reports an adapter that omits it. Set it to `None` in `__init__` and
assign the real loop in `__aenter__`.

## How resolution works

Listing entry points never imports the target module. The module loads only
when a name is resolved, so installing many vendor packages stays cheap. An
unknown name raises `ProviderNotRegisteredError` or `AdapterNotRegisteredError`
with the requested name and the names that are installed:

```text
No coordination adapter registered as 'mongo' in the
'grelmicro.coordination.adapters' entry-point group. Available: kubernetes,
memory, postgres, redis, sqlite. Install the package that ships it, or check
the name.
```
