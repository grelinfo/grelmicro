# Frameworks

grelmicro is not a web framework. It plugs into the one you picked, and
`micro.install(app)` detects which one from the app object.

This is the explicit list of what that call supports.

| Framework | Extra | What `install` wires |
|---|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | `grelmicro[fastapi]` | The lifespan, the per-request binding, problem details, and OpenTelemetry auto-instrumentation. |
| [Starlette](https://www.starlette.io/) | `grelmicro[starlette]` | The lifespan, the per-request binding, and problem details. |
| [Litestar](https://litestar.dev/) | `grelmicro[litestar]` | The startup and shutdown hooks, the per-request binding, and problem details. |
| [FastStream](https://faststream.airt.ai/) | `grelmicro[faststream]` | The startup and shutdown hooks, the per-message binding, and the broker telemetry middleware. |

Problem details mean every rejection grelmicro raises answers the client as an
RFC 9457 `application/problem+json` body instead of becoming a `500`. Read
[Problem Details](http/problems.md), or pass `problem_details=False` to answer
them yourself. FastStream serves no HTTP, so the flag does nothing there.

The extra installs the framework itself. grelmicro imports nothing from it
until you call `install`, so an app that already depends on FastAPI needs no
extra at all.

Whatever the framework, `install` does the same two things: it opens
`async with micro:` alongside the framework's own lifecycle, so components are
ready before the first request, and it binds the app around each handler, so
`Lock("cart")` and `RateLimiter.sliding_window(...)` resolve their backend with
no `backend=` argument. Read [Wiring an App](wiring.md) for what that means in
practice.

## FastAPI and Starlette

```python
from fastapi import FastAPI

from grelmicro import Grelmicro

micro = Grelmicro(uses=[...])
app = FastAPI()
micro.install(app)
```

FastAPI adds two optional pieces: a [health router](health.md) you include
yourself, and the [idempotency middleware](idempotency/middleware.md).

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

`GrelmicroMiddleware` is pure ASGI. It ships with the Starlette integration and
runs in front of any ASGI app. Open `micro` in whatever lifecycle the framework
gives you, then wrap the app yourself:

```python
from grelmicro.integrations.fastapi import GrelmicroMiddleware

app = GrelmicroMiddleware(app, micro=micro)
```

`micro.install(app)` raises `TypeError` for a framework it does not know,
naming the ones it does. A package can register its own integration under the
`grelmicro.integrations` entry point group, and `install` then finds it like
any first-party one. See [Plugins](architecture/plugins.md).

## Not tied to a framework

Some pieces are pure ASGI and work anywhere, whether or not `install` knows
your framework:

- [`ClientAddressMiddleware`](security/clientip.md) resolves the real caller
  behind a reverse proxy.
- [`GrelmicroMiddleware`](reference/fastapi.md) binds the app per request.

Everything else, the locks, the cache, the outbox, the scheduler, and the
resilience patterns, is plain async Python and needs no framework at all.
