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
a backend ambiently runs inside the request scope. `install_route_gate` and
`route_declarations` declare your routes, as
[Declare the routes](#declare-the-routes) shows.

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
| `methods` | The methods the router answers on this route, in capitals. A router that answers `HEAD` for a `GET` lists both. `None` is every method, as a mount or a websocket route answers | `None` |
| `anonymous` | The route serves a caller with no credential | `False` |
| `scopes` | The scopes the caller must hold, every one of them | empty |
| `own_checks` | The route runs checks of its own before the handler, so its answer can depend on the caller: a dependency, a guard, or middleware on the route or on a mount around it. A response is never cached or replayed across callers | `False` |
| `cache` | `CachedResponses` may store the route's response. `True` keeps it for the TTL the component is configured with, a `timedelta` keeps it that long, and a number is refused | `False` |

A declaration is frozen. Pass `path` first and every other field by keyword.
A route whose methods declare differently, such as a public `GET` beside a
protected `POST`, lists one declaration per method set.

`install_route_gate(app, gate)` wraps what the router dispatches to. `gate`
is a `Gate`, from `grelmicro.http`. For each
thing your router dispatches to, call `gate(app, *declarations)` at install
and dispatch to the ASGI app it returns. Pass one declaration, or one per
method set. The returned app:

- picks the declaration of the request's method. A method no declaration
  names needs a caller,
- refuses a request the declaration does not admit: a `401` with its
  challenge, a `403` for a missing scope, or a websocket denial, recorded as a
  security event with the declaration's path,
- runs the handler of a request it admits, inside the rate limit, the response
  cache, idempotency and conditional requests the app registered.

Pass `name=`, a function of the request's ASGI scope, to name a refusal by
another path, such as the mounts the request came through. Pass `door=True`
for the entrance of a subtree whose routes you gate too, such as a mounted
app you walk into: it refuses as a route's gate does, and leaves the rate
limit, the cache and idempotency to the routes inside, so they run once.

Gating an app that `gate` returned already counts its declarations and
returns it as it is, so a route shared by two apps, or reached under two
paths, is gated once. `gate`
does no I/O, because the credential was verified before routing, and it raises
for a declaration that cannot hold, so a wrong route fails at install. A rule
that needs I/O, such as a lookup by the caller, belongs in the handler or in
the framework's own guard.

A mount whose routes you cannot read is one route. Declare its path with
`methods=None` and nothing else, and everything under it stays authenticated.
A router included more than once is gated at every path it is included under.

`route_declarations(app)` lists the same declarations. `micro.install(app)`
refuses to start an app with a listed route that carries no gate, so a route
added after install never serves ungated. `grelmicro check`,
`micro.describe(app)` and the OpenAPI document read the list too. Build both
from one function, so they cannot disagree. Test them against real routes:
an included router, a mount and a class-based endpoint are where a missed
`scopes` hides, and it lets any authenticated caller in.

For Starlette, where your own decorators set `anonymous` and `scopes` on the
endpoint:

```python
from starlette.routing import Mount, Route, WebSocketRoute

from grelmicro.http import RouteDeclaration


def route_declarations(app):
    return [_declare(route) for route in _routes(app)]


def install_route_gate(app, gate):
    for route in _routes(app):
        route.app = gate(route.app, _declare(route))


def _routes(app):
    kinds = (Route, WebSocketRoute, Mount)
    return [route for route in app.routes if isinstance(route, kinds)]


def _declare(route):
    methods = getattr(route, "methods", None)
    endpoint = getattr(route, "endpoint", None)
    return RouteDeclaration(
        route.path,
        methods=frozenset(methods) if methods else None,
        anonymous=getattr(endpoint, "anonymous", False),
        scopes=frozenset(getattr(endpoint, "scopes", ())),
    )
```

A mount is gated as one protected route here. A real integration walks into
mounts it can read, and declares each `HTTPEndpoint` method on its own.

### The rules

- **Deny by default.** A route is served without a credential only when it
  declares `anonymous=True`, or when its path is in `exclude=`. A declaration
  with nothing but a path is an authenticated route.
- **An excluded path is never authenticated.** Its gate lets every request
  through, and a token sent to it is not read. The request is held to the
  route the router dispatched it to, so a path rewritten on the way cannot
  leave `exclude=`.
- **A gate needs the app's policy.** The middleware of the app that
  authenticates a request puts its policy on the request. A gated route
  reached without it, through a scope rebuilt on the way or from an app
  without authentication, is refused `401`. So a router is not shared with an
  app that does not authenticate it.
- **A request without a credential is routed only where it can be served.**
  An app with an `anonymous=True` route routes it, and any other refuses it
  before routing. An anonymous route reached without the app's policy is
  refused `401` as well.
- **A URL no route answers is `401`.** A request with no credential that
  reaches no gate gets the same `401` and body as a protected route, in place
  of the `404`, `405`, slash redirect or error. The challenge names no scopes,
  on any route, so a caller without a credential cannot tell which routes
  exist.
- **Some answers come before routing.** A CORS preflight, and a response your
  app's own middleware writes before routing, reach the caller unchanged. So is a
  CORS answer to a preflight that middleware under a mount writes, since a
  browser sends no credential on a preflight.
- **Nothing is spent on a refusal.** The rate limit, the response cache and
  idempotency run once a gate admitted the request, so a request a route
  refuses spends no budget, costs no cache lookup and stores nothing. A
  request no route answers reaches none of them either.
- **A credential is verified before routing.** Outside `exclude=`, a token
  that does not verify, or a caller `bans` refuses, is answered before the
  framework sees the request. That holds on an `anonymous=True` route too: a
  token that is sent must verify.
- **No hooks, no declarations.** An integration without the two functions
  declares nothing. Every route stays authenticated, and `CachedResponses`
  caches only the paths `include=` names.
- **Some declarations are refused.** These fail at install, naming the
  route: `anonymous=True` with `scopes`, `cache` with `own_checks`, `cache` on
  a route answering a method other than `GET` or `HEAD`, an empty `methods`,
  and a method in lower case. A `CachedResponse` declared on a router covers
  the reads under it that run no checks of their own, so its writes and those
  reads declare no `cache`.
- **A cached protected route is shared.** Its response is served to every
  caller the route admits, so declare `cache` only where each of them gets the
  same answer.

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
