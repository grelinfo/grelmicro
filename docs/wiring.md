# Wiring an App

A real app swaps the memory backend for a shared service and runs the patterns
behind a web framework. This page wires one provider, then installs the app into
FastAPI and FastStream with one call.

## One provider, one line

A provider owns the connection. Pass it to `uses=` and grelmicro registers a
default component for every kind the provider serves:

```python
from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[redis])
```

Now `Lock`, `Cache`, and `RateLimiter` all resolve the Redis backend with no
extra wiring.

!!! warning
    Keep connection URLs in environment variables, not inline like the example
    above. The [Configuration](config.md) page shows the deployment story.

## Add patterns

Build the patterns you need and use them inside the app scope:

```python
from grelmicro.coordination import Lock

lock = Lock("cart")

async with micro:
    async with lock:
        ...
```

The lock finds the registered Redis backend through the active app. No `backend=`
argument needed.

## FastAPI

Call `micro.install(app)`. One call wires both pieces:

```python
--8<-- "simple_fastapi_app.py"
```

The lifecycle is always required. `install` always wires it: it opens `micro`
once at startup and closes it at shutdown, so every component is ready before
the first request. A lifespan you already pass to `FastAPI(lifespan=...)` keeps
running, chained around `micro`.

The per-handler ambient binding is optional. `install` wires it by default, so
patterns like `Lock("cart")` and `RateLimiter.sliding_window(...)` resolve their
backends inside route handlers with no `backend=` argument. Pass `ambient=False`
when your handlers always pass an explicit `backend=` and do not need it:

```python
micro.install(app, ambient=False)
```

## FastStream

The same call wires a FastStream app:

```python
from faststream import FastStream
from faststream.redis import RedisBroker

from grelmicro import Grelmicro
from grelmicro.coordination import Lock

broker = RedisBroker("redis://localhost:6379/0")
micro = Grelmicro(uses=[...])

app = FastStream(broker)
micro.install(app)


@broker.subscriber("orders")
async def handle(order: dict) -> None:
    async with Lock("orders"):
        ...
```

`install` opens `micro` on startup, closes it after shutdown, and binds the app
around each consumed message so patterns resolve inside subscribers. Pass
`ambient=False` to skip the per-message binding.

## Next

Read the per-pattern pages for [cache](cache.md), [coordination](coordination.md),
[scheduling](task.md), and [resilience](resilience/index.md). When you deploy,
the [Configuration](config.md) page shows how to tune every pattern with `GREL_*`
environment variables.
