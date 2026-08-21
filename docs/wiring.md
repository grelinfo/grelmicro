# Wiring an App

A real app runs its patterns behind a web framework. This page wires one
provider, then installs the app into FastAPI, FastStream, and Litestar with one
call.

## One provider, one line

A provider owns the connection. Pass it to `uses=` and grelmicro registers a
default component for every kind the provider serves, `Outbox` aside:

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

## Register something conditionally

A component that exists only for one backend or one environment stays inline. A
`None` entry in `uses=` is skipped:

```python
--8<-- "wiring/conditional.py"
```

With `STORE_BACKEND` unset, the app registers the health checks alone. Set it to
`redis` and the provider joins them, with no change to the shape of the list.

When the list is long enough to deserve its own function, annotate it with
`Usable`. It names everything `uses=` accepts, which `Component` does not: a
`Provider` is not a `Component`, and neither is a plain async context manager.

```python
--8<-- "wiring/usable.py"
```

`Usable` names one item, not the list. Keep the conditional in an `if` and the
annotation stays `list[Usable]`. A prebuilt list that carries its own `None`
entries is `list[Usable | None]`, which `uses=` accepts just the same.

`micro.use(item)` registers one item after construction and rejects `None`,
because a single call can be guarded with `if` instead:

```python
if os.getenv("STORE_BACKEND") == "redis":
    micro.use(RedisProvider())
```

!!! note "A Provider fills the kinds nothing else claims"
    A Provider registers a default component for every kind it serves that no
    component already claims, whether you list it in `uses=` or pass it to
    `micro.use(provider)`. Explicit wins, the provider fills the rest.

    `Outbox` is the exception, and it is always written out. It carries the
    handlers your messages are delivered to and the relay that delivers them,
    so it belongs where those are declared. See [Outbox](outbox/index.md).

    Two providers are the one case where the forms differ. `uses=[p1, p2]` sees
    both at once and raises `AmbiguousProviderError`, because neither can be the
    default for a kind they share. `micro.use(p1)` then `micro.use(p2)` is
    ordered, so `p1` fills its kinds first and `p2` finds them claimed and fills
    only what is left. Register the components explicitly when you want two
    providers to serve different kinds.

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

`install` also wires what you registered. `ErrorResponses()` has grelmicro's
rejections answered as problem details, and `IdempotentRequests()` adds the
idempotency middleware and describes it in the OpenAPI schema. Each happens
only because the component is in `uses=[...]`. The
[Frameworks](frameworks.md) page lists what every framework gets.

!!! warning "Always call `install`, never hand-wire the lifespan alone"
    A request handler runs in its own task, so it only resolves ambient backends
    when `install` adds the middleware. If you open `async with micro:` in a
    hand-written lifespan but forget `install` (or pass `ambient=False`), the app
    starts up healthy and then every ambient call raises `OutOfContextError` on
    the first request that hits it. `install(ambient=False)` warns at startup when
    ambient components are registered (it raises under `Grelmicro(strict=True)`),
    and you can assert the wiring in a test before it ships:

    ```python
    def test_ambient_binding_is_wired() -> None:
        assert micro.check_ambient_binding(app)
    ```

!!! danger "A mounted sub-application does not fail loudly"
    Install every app that owns components, mounted ones included. A mount is
    an ordinary call in the same task, so the host's request scope is still
    bound inside the sub-application. A sub-application that forgot `install`
    therefore resolves against the **host's** components instead of raising:

    ```python
    host_micro = Grelmicro(uses=[Cache(host_backend)])
    sub_micro = Grelmicro(uses=[Cache(sub_backend)])

    host = FastAPI()
    host_micro.install(host)

    sub = FastAPI()  # install(sub) forgotten
    host.mount("/sub", sub)
    ```

    A write from `sub` lands in `host_backend`. `sub_backend` stays empty, and
    nothing reports it. Two applications that look isolated share one store.

    This is the one case where a forgotten `install` does not raise
    `OutOfContextError`, because a binding is present, just the wrong one.
    Assert each app separately:

    ```python
    def test_every_app_is_wired() -> None:
        assert host_micro.check_ambient_binding(host)
        assert sub_micro.check_ambient_binding(sub)  # False when install is missing
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
async def handle(order: Order) -> None:
    async with Lock("orders"):
        ...
```

`install` opens `micro` on startup, closes it after shutdown, and binds the app
around each consumed message so patterns resolve inside subscribers. Pass
`ambient=False` to skip the per-message binding.

## Litestar

The same call wires a Litestar app. Litestar builds its middleware stack when
you construct the app, so call `install` after it:

```python
from litestar import Litestar, get

from grelmicro import Grelmicro
from grelmicro.coordination import Lock

micro = Grelmicro(uses=[...])


@get("/carts")
async def carts() -> dict[str, str]:
    async with Lock("cart"):
        return {"status": "ok"}


app = Litestar(route_handlers=[carts])
micro.install(app)
```

Hooks and lifespan managers already passed to `Litestar(...)` keep running.
The [Frameworks](frameworks.md) page lists every framework `install` supports.

## See what got wired

`micro.describe()` answers what the app is wired with: every component, the
backend behind it, the provider it borrows, and the checks that passed or
failed. Credential-like values are masked.

```python
report = micro.describe()

assert report.ok
assert [component.kind for component in report.components] == ["cache"]
```

The same report runs from the command line, which exits non-zero when a check
fails so CI can gate on it:

```bash
python -m grelmicro check app:micro
```

```
Environment: production

Components
  coordination/default    RedisLockAdapter, RedisScheduleAdapter <- redis
  cache/default           RedisCacheAdapter <- redis

Providers
  redis  (RedisProvider)  redis://user:***@localhost:6379/0
    env:      REDIS_*
    serves:   lock, readwritelock, leaderelection, schedule, cache, ratelimiter, circuitbreaker
    declines: outbox

Checks
  ok    backend-scope: every bound backend reaches as far as its component requires
```

The `declines` line is the one to read when a pattern is not wired the way you
expected. A provider fills a default component only for the kinds it serves, so
`uses=[redis]` leaves the outbox unwired and this is where that shows up.

Pass the web application to include the ambient binding check, which catches
the forgotten `install` described above, mounted sub-applications included:

```python
def test_every_app_is_wired() -> None:
    assert micro.describe(app).ok
```

## Next

Read the per-pattern pages for [cache](cache/index.md), [coordination](coordination/index.md),
[scheduling](task.md), and [resilience](resilience/index.md). When you deploy,
the [Configuration](config.md) page shows how to tune every pattern with `GREL_*`
environment variables.
