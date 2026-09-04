# Frameworks

grelmicro is not a web framework. It plugs into the one you picked, and
`micro.install(app)` detects which one from the app object.

**Every pattern and every component works on every framework grelmicro
supports.** A `Lock`, a `RateLimiter`, `@cached`, the outbox, the scheduler,
health checks, logging, tracing, and metrics behave the same whichever one you
picked. What differs is the wiring `install` does for you, and three pieces
that only FastAPI can have. Both halves of that are named below, and a test
holds them.

## What `install` wires

| Framework | Extra | What `install` wires | Not portable |
|---|---|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | `grelmicro[fastapi]` | The lifespan, the per-request binding, the error responses, the middleware you registered, and the OpenAPI schema. | Nothing. |
| [Starlette](https://www.starlette.io/) | `grelmicro[starlette]` | The lifespan, the per-request binding, the error responses, and the middleware you registered. | The [health router](health.md), the metrics router, and the OpenAPI annotations. |
| [Litestar](https://litestar.dev/) | `grelmicro[litestar]` | The startup and shutdown hooks, the per-request binding, the error responses, and the middleware you registered. | The same three, and request auto-instrumentation, which Litestar does with its own plugin. |
| [FastStream](https://faststream.airt.ai/) | `grelmicro[faststream]` | The startup and shutdown hooks, the per-message binding, and the broker telemetry middleware. | Everything HTTP, because FastStream serves none. |

`install` also wires what you registered. `ErrorResponses()` has grelmicro's
rejections answered as RFC 9457 responses, `IdempotentRequests()` has repeated
requests replayed, and `Trace()` has requests auto-instrumented. Each happens
only because the component is in `uses=[...]`.

Whatever the framework, `install` does the same two things: it opens
`async with micro:` alongside the framework's own lifecycle, so components are
ready before the first request, and it binds the app around each handler, so
`Lock("cart")` and `RateLimiter.sliding_window(...)` resolve their backend with
no `backend=` argument. Read [Wiring an App](wiring.md) for what that means in
practice.

Nothing else about how your framework answers a request changes. The
per-request binding adds one middleware that sets a context variable, and
changes nothing a client can see.

The extra installs the framework itself. grelmicro imports nothing from it
until you call `install`, so an app that already depends on FastAPI needs no
extra at all.

## The one exception

Only FastAPI builds an OpenAPI schema, so one piece stops at its door:

| Piece | Why | Where it is going |
|---|---|---|
| `document_idempotency(app)` | Annotates an OpenAPI schema. | Nowhere. A framework that builds no schema has nothing to annotate. |

The middleware behind it is portable. `IdempotentRequests()` wires on FastAPI,
Starlette, and Litestar alike, and only the schema annotation is skipped where
there is no schema.

[`health_router()`](health.md) and [`metrics_router()`](metrics.md) build a
FastAPI `APIRouter`, so they are FastAPI's door onto the endpoints rather than
the only one. Every framework has the same endpoints through
[`health_asgi()`](health.md#without-a-web-framework) and `metrics_asgi()`,
which are pure ASGI, and a process with no framework at all serves them on its
own port with [`OpsServer`](http/server.md). All three doors render through
one set of functions, and `tests/test_endpoint_parity.py` holds them to one
status, one set of headers, and one body.

FastStream is not a second exception, it is a different axis. It serves no
HTTP, so every HTTP-shaped behaviour is out of scope for it by definition. It
carries no HTTP-facing hook, so `micro.install(app)` skips those without any
code anywhere reading a framework's name. Everything else, the locks, the
cache, the outbox, the scheduler, and the resilience patterns, behaves inside a
subscriber exactly as it does inside a request handler.

## How the claim is held

`tests/test_framework_parity.py` walks the package for every pattern that
resolves through the active app, refuses to pass on an empty scan, and runs
each one inside a request handler on FastAPI, Starlette, and Litestar and
inside a subscriber on FastStream. The values have to match, not merely the
absence of an exception.

For what reaches the wire, the bar is higher: the status line, every header,
and the body byte for byte, so a client cannot tell which framework answered.
A rejection and an idempotent replay are both held to it.

A pattern added tomorrow is covered without anyone remembering to add it, and a
piece that cannot be portable has to say so in the table above.

## FastAPI and Starlette

```python
from fastapi import FastAPI

from grelmicro import Grelmicro

micro = Grelmicro(uses=[...])
app = FastAPI()
micro.install(app)
```

FastAPI adds two optional pieces: a [health router](health.md) you include
yourself, and the [metrics router](metrics.md).

## Litestar

Litestar builds its middleware stack when you construct the app, so call
`install` after it:

```python
from litestar import Litestar

from grelmicro import Grelmicro

micro = Grelmicro(uses=[...])
app = Litestar(route_handlers=[...])
micro.install(app)
```

Hooks and lifespan managers already passed to `Litestar(...)` keep running.
The binding wraps the app's ASGI handler, so it sits outside every middleware
Litestar built and any of them that resolves a backend still runs inside the
request scope.

Litestar carries its own OpenTelemetry plugin, configured at construction, so
`install` does not instrument requests for you. `Trace` still exports the spans
your own code emits.

## FastStream

```python
from faststream import FastStream
from faststream.redis import RedisBroker

from grelmicro import Grelmicro

micro = Grelmicro(uses=[...])
app = FastStream(RedisBroker("redis://localhost:6379/0"))
micro.install(app)
```

The binding runs per consumed message, so patterns resolve inside subscribers.

## Any other ASGI framework

`GrelmicroMiddleware` is pure ASGI and runs in front of any ASGI app. Open
`micro` in whatever lifecycle the framework gives you, then wrap the app
yourself:

```python
from grelmicro import GrelmicroMiddleware

app = GrelmicroMiddleware(app, micro=micro)
```

`micro.install(app)` raises `TypeError` for a framework it does not know,
naming the ones it does. A package can register its own integration under the
`grelmicro.integrations` entry point group, and `install` then finds it like
any first-party one. See [Plugins](architecture/plugins.md).

## Not tied to a framework

Some pieces are pure ASGI and work anywhere, whether or not `install` knows
your framework:

- [`GrelmicroMiddleware`](reference/app.md) binds the app per request.
- [`IdempotencyMiddleware`](http/idempotency.md) replays a stored
  response when a request repeats its key.
- [`ClientAddressMiddleware`](security/clientip.md) resolves the real caller
  behind a reverse proxy.
- [`health_asgi()`](health.md#without-a-web-framework) and `metrics_asgi()`
  serve the probes and the scrape target, mounted in any ASGI app.
- [`OpsServer`](http/server.md) serves those same endpoints on a port of its
  own, for a process that runs no web framework at all.

Everything else, the locks, the cache, the outbox, the scheduler, and the
resilience patterns, is plain async Python and needs no framework at all.
