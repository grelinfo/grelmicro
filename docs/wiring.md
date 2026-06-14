# Wiring an App

A real app swaps the memory backend for a shared service and runs the patterns
behind a web framework. This page wires one provider and the FastAPI middleware.

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

Open the app in the lifespan and add the middleware. The middleware binds the
active app to each request, so patterns resolve their backends inside route
handlers.

```python
--8<-- "simple_fastapi_app.py"
```

The lifespan opens `micro` once at startup and closes it at shutdown. Every route
that uses a pattern resolves through the app the middleware bound.

## Next

Read the per-pattern pages for [cache](cache.md), [coordination](coordination.md),
[scheduling](task.md), and [resilience](resilience/index.md). When you deploy,
the [Configuration](config.md) page shows how to tune every pattern with `GREL_*`
environment variables.
