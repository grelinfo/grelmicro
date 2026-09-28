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

The others are optional, and `install` feature-detects each with `getattr`:

```python
def install_error_responses(app, errors) -> None: ...
def install_middleware(app, components) -> None: ...
def install_route_gate(app, gate) -> None: ...
def route_declarations(app) -> Iterable[RouteDeclaration]: ...
```

`install_error_responses` answers every rejection in the format the registered
`ErrorResponses` carries. `install_middleware` receives the registered
components that carry `asgi_middleware()`, which returns the middleware class
and the arguments to build it with, and adds each one the way your framework
takes a middleware. Keep the binding outermost, so a middleware that resolves
a backend ambiently runs inside the request scope. The last two declare your
routes, as [Declare the routes](#declare-the-routes) shows.

Leave them out for a framework that serves no HTTP. Nothing anywhere reads a
framework's name to decide, so an absent attribute is the whole answer.

Declare it the same way as a Provider:

```toml
[project.entry-points."grelmicro.integrations"]
sanic = "grelmicro_sanic:integration"
```

### Declare the routes

`AuthenticatedRequests` decides per route, once the framework has matched the
request. Your integration tells it what each route requires with a
`RouteDeclaration`, from `grelmicro.http`:

| Field | What it says | Default |
|---|---|---|
| `path` | The path template, as the router matches it, such as `/orders/{order_id}` | required |
| `methods` | The methods the route answers. `GET` covers `HEAD`. `None` is every method, as a mount or a websocket route answers | `None` |
| `anonymous` | The route serves a caller with no credential | `False` |
| `scopes` | The scopes the caller must hold, every one of them | empty |
| `runs_checks` | The route runs checks of its own before the handler, such as a dependency or a guard, so its answer can depend on the caller | `False` |
| `cache_ttl` | Seconds `CachedResponses` keeps the route's response. `None` leaves the route to `include=` | `None` |

A declaration is frozen. Pass `path` first and every other field by keyword.

`install_route_gate(app, gate)` wraps every route the router dispatches to.
Call `gate(declaration)` once per route, at install. It returns a check. Call
the check with the request's ASGI scope before the handler runs. The check
does no I/O, because the credential was verified before routing. It raises
`AuthenticationRequiredError` for a caller with no credential and
`InsufficientScopeError` for one lacking a scope. Let them reach the handlers
`install_error_responses` added, which answer `401` and `403`. `gate` itself
raises for a declaration that cannot hold, so a wrong route fails at install.

A mount whose routes you cannot read is one route. Declare its path with
`methods=None` and nothing else, and everything under it stays authenticated.

`route_declarations(app)` lists the same declarations. `micro.install(app)`
refuses to start an app with a listed route that carries no gate, so a route
added after install never serves ungated. `grelmicro check`,
`micro.describe(app)` and the OpenAPI document read the list too. Build both
from one function, so they cannot disagree.

For Starlette, where your own decorators set `anonymous` and `scopes` on the
endpoint:

```python
from starlette.routing import Route, WebSocketRoute

from grelmicro.http import RouteDeclaration


def route_declarations(app):
    return [_declare(route) for route in _routes(app)]


def install_route_gate(app, gate):
    for route in _routes(app):
        route.app = _gated(route.app, gate(_declare(route)))


def _routes(app):
    return [r for r in app.routes if isinstance(r, (Route, WebSocketRoute))]


def _declare(route):
    methods = getattr(route, "methods", None)
    return RouteDeclaration(
        route.path,
        methods=frozenset(methods) if methods else None,
        anonymous=getattr(route.endpoint, "anonymous", False),
        scopes=frozenset(getattr(route.endpoint, "scopes", ())),
    )


def _gated(asgi, check):
    async def gated(scope, receive, send):
        check(scope)
        await asgi(scope, receive, send)

    return gated
```

A real integration also walks mounts and reads each `HTTPEndpoint` method.

### The rules

- **Deny by default.** A route is served without a credential only when it
  declares `anonymous=True`. A declaration with nothing but a path is an
  authenticated route.
- **A URL no route answers is `401`.** A request with no credential that
  reaches no gate gets the same `401` and body as a protected route, in place
  of the `404`, `405` or slash redirect. So a caller without a credential
  cannot tell which routes exist.
- **Some answers come before routing.** A CORS preflight, and a response your
  app's own middleware writes before routing, are answered as today.
- **A credential is verified before routing.** A token that does not verify,
  or a caller `bans` refuses, is answered before the framework sees the
  request, on every route.
- **No hooks, no declarations.** An integration without the two functions
  declares nothing. Every route stays authenticated, and `CachedResponses`
  caches only the paths `include=` names.
- **Some declarations are refused.** `anonymous=True` with `scopes`, and a
  `cache_ttl` on a method that is not a read, fail at install, naming the
  route.

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

### What grelmicro promises your adapter

These rules say how the protocols may change, so an adapter written today
keeps working.

**An unsupported algorithm raises.** `RateLimiterBackend.bind` and
`CircuitBreakerBackend.bind` receive a config from a union that grows as new
algorithms land. Your backend supports the kinds it knows and ends the match
by raising `NotImplementedError` naming the kind it was handed. Never fall
through to a default: a silent one turns a config the operator asked for into
a different algorithm running in production. Dispatch on the kind before you
touch the client, so an unsupported kind fails without a connection.

**Result tuples grow by name.** `RateLimitResult` and `CircuitBreakerSnapshot`
may gain fields with defaults. Read them by attribute, never by unpacking the
whole tuple, or a new field breaks your call site.

**`RouteDeclaration` grows by keyword.** It may gain fields with defaults, and
a new one is off by default. A declaration built with keywords keeps its
meaning.

**Integration signatures are frozen.** grelmicro never adds an argument to
`install(app, micro, *, ambient=True)`, `is_bound(app)` or any optional
function. A new capability arrives as a new optional module attribute that
grelmicro feature-detects, so an older integration keeps loading.
`install_error_responses`, `install_middleware`, `install_route_gate` and
`route_declarations` arrived that way.

**`ClockBackend` is complete.** It stays at `monotonic` and `sleep`.
Wall-clock time is out of scope, which is why a cron schedule reads the system
clock directly rather than through a backend.

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
