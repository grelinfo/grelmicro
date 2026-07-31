# Idempotency

The `idempotency` module makes a retried operation safe to run more than once. It stores the response under a caller-supplied key. A repeated key within the configured lifetime replays the stored response instead of running the operation again.

This pairs with retries. Wrap a call in `Retry`, mark the operation idempotent, and a retry that lands after the first attempt already succeeded returns the stored response rather than charging the card twice.

- **[Idempotency](#the-block-form)**: an explicit block that runs the work once and replays it on repeat.
- **[@idempotent](#decorator)**: a decorator that derives the key from the call arguments.
- **[IdempotencyMiddleware](#http-middleware)**: replays a whole HTTP response when a request repeats its `Idempotency-Key` header.

## Quick start

A FastAPI handler reads the key from the `Idempotency-Key` header and wraps the work in a block. The Memory backend needs no extra service, so this runs as-is. Swap in Redis or Postgres for production.

```python
from typing import Annotated

from fastapi import FastAPI, Header
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.idempotency import Idempotency

micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
app = FastAPI()

idem = Idempotency("charge", ttl=3600)


@app.post("/charge")
async def charge(
    amount: int,
    key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async with idem(key) as op:
        if op.replayed:
            return op.result()
        response = await do_charge(amount)
        op.store(response)
        return response
```

The first request with a given key runs `do_charge` and stores the response. Any later request with the same key replays that response without charging again.

## HTTP middleware

The quick start reads the key and opens a block in every handler that needs one. `IdempotencyMiddleware` does it once, for the whole app.

```python title="middleware.py"
--8<-- "idempotency/middleware.py"
```

Handlers stay as they are. There is no key parameter to thread through and no block to open.

Add the middleware before or after `micro.install(app)`. It resolves its `Cache` through the grelmicro request scope, and `install` keeps that scope outside every other middleware.

### What a client sees

The first request runs the handler and gets the ordinary response:

```http
POST /charge?amount=100 HTTP/1.1
Idempotency-Key: 5f9d2c1e

HTTP/1.1 200 OK
content-type: application/json

{"amount":100}
```

The retry never reaches the handler:

```http
POST /charge?amount=100 HTTP/1.1
Idempotency-Key: 5f9d2c1e

HTTP/1.1 200 OK
content-type: application/json
idempotent-replayed: true

{"amount":100}
```

Same status, same body, and the same headers the handler set. The `idempotent-replayed` header is the one addition, so a client can tell a replay from a fresh run.

A request without the header passes straight through. Adding the middleware changes nothing until a client opts in.

### Errors replay too

Every response the app returns is stored, `4xx` and `5xx` included. A handler that writes to the database and then fails on the way out leaves a stored failure, so the retry gets that instead of running the write again.

Store only what you want replayed. A transient `503` stored for the whole `ttl` keeps answering `503` long after the dependency recovers, so exclude the statuses that mean "try again later":

```python
app.add_middleware(
    IdempotencyMiddleware,
    idempotency=Idempotency("http"),
    skip=lambda response: response["status"] in (429, 502, 503, 504),
)
```

A handler that raises an unhandled exception is different. The framework turns it into a `500` outside the middleware, so nothing is stored and a retry runs fresh. A raised `HTTPException` is not an unhandled exception. The framework turns it into a response inside the middleware, so it is stored and replayed like any other.

### What is never stored

Four kinds of response, and each one lets a retry run the handler again:

| Response | Reason |
|---|---|
| Carries `Set-Cookie` | Replaying it hands one caller another caller's session. |
| Carries `Content-Encoding` | The stored bytes would reach a client that never negotiated that encoding. Add the middleware before any compression middleware. |
| Declares trailers | Trailers cannot be replayed faithfully. |
| Body over `max_body_size` (1 MiB) | A large response passes through instead of being held in memory. |

All four are logged at warning level. Watch for those warnings: a response that was not stored means the next retry runs the handler again.

Everything else is stored whatever its content type, so a `201 Created` with an empty body and a `Location` header replays like any other response.

The response streams to the client as the handler produces it. The copy is buffered alongside and written to the cache after the last chunk is sent.

### Custom rules with `skip`

`skip` receives the finished response and returns `True` to leave it unstored. It mirrors [`skip` on `@cached`](cache.md#decorator-parameters).

```python
app.add_middleware(
    IdempotencyMiddleware,
    idempotency=Idempotency("http"),
    skip=lambda response: response["status"] == 202,
)
```

Use it for a response that is technically replayable but should not be. The four rules above run first, so `skip` never has to re-check them.

### Keys are scoped per route

The stored key combines the method, the path, the query string, and the header value. The same client key on `POST /charge` and `POST /refund` stores two entries, so one route never replays another one's response.

!!! warning "Set `key_maker` in a multi-tenant app"
    Without it, any client that learns another client's key replays their response, body included. Fold the caller identity into the key.

    ```python
    def tenant_key(scope, key):
        tenant = dict(scope["headers"])[b"x-tenant"].decode()
        return f"{tenant}|{scope['path']}|{key}"


    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        key_maker=tenant_key,
    )
    ```

    Read the identity from wherever your authentication puts it. `scope["user"]` works only when an authentication middleware runs outside this one.

`key_maker` receives the ASGI scope and the client's key, and returns the whole stored key. It mirrors [`key_maker` on `@cached`](cache.md#custom-keys).

### Duplicates in flight

A duplicate that arrives while the first execution is still running waits for it, then replays its response.

The wait folds duplicates across replicas when a `Coordination` lock backend is configured, and in-process otherwise. See [Single-flight duplicates](#single-flight-duplicates).

The wait is bounded by `wait_timeout`, ten seconds by default:

```http
HTTP/1.1 409 Conflict
retry-after: 1

{"detail": "A request with this Idempotency-Key is still in flight. Retry shortly."}
```

Past the timeout the duplicate receives that instead of holding the connection open.

### Conflicting payloads

Pass `fingerprint_body=True` to hash the request body and store the hash with the response. A key reused with a different body then gets `422 Unprocessable Content` instead of a wrong replay, matching the [Idempotency-Key header draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/).

```python
app.add_middleware(
    IdempotencyMiddleware,
    idempotency=Idempotency("http"),
    fingerprint_body=True,
)
```

Fingerprinting buffers the request body before the handler runs, so it is off by default. A request body over `max_body_size` is answered with `413`.

### OpenAPI

The middleware runs outside the routing layer, so nothing it does reaches the generated schema. A client built from that schema never learns the header exists. `document_idempotency` fixes that:

```python
from grelmicro.integrations.fastapi import (
    IdempotencyMiddleware,
    document_idempotency,
)

micro.install(app)
app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
document_idempotency(app)
```

Every operation the middleware covers gains the `Idempotency-Key` header parameter and the responses the middleware itself returns. Call it any time after `add_middleware`, and routes added afterwards are covered too.

An operation that already declares the header keeps its own declaration. The `422` that FastAPI generates for request validation keeps its schema and gains the idempotency case in its description, so neither is lost.

!!! note "Mounted sub-applications"
    A mounted sub-application builds its own schema. Call `document_idempotency` on it as well.

### Background tasks

A background task runs after the response is sent, so the response is stored and replayable while the task is still working. A replay can land before the original request's background work finishes. Put anything a retry must not repeat in the handler, not in a background task.

### Middleware parameters

| Parameter | Default | Behaviour |
|---|---|---|
| `idempotency` | required | The `Idempotency` that stores responses. Its `ttl` sets the replay window. |
| `header` | `"Idempotency-Key"` | Request header carrying the key. |
| `methods` | `("POST",)` | Methods that take a key. Every other method passes through. |
| `key_maker` | `None` | Build the stored key from the scope and the client key. Set it in any multi-tenant app. |
| `skip` | `None` | Predicate over the finished response. Return `True` to not store it. |
| `require_key` | `False` | Answer `400` when a matched method arrives without the header. |
| `fingerprint_body` | `False` | Hash the request body and answer `422` on a reused key with a different body. |
| `max_body_size` | `1048576` | Largest body held in memory, in bytes. Caps the stored response, and the fingerprinted request. |
| `wait_timeout` | `10.0` | Seconds a duplicate waits for an execution in flight before `409`. |

## Storage

Responses ride the cache layer. Pass an explicit `cache=` to bind a `TTLCache`, or leave it unset to resolve the active app's `Cache` component backend. Without either, the first call raises an out-of-context error.

```python
from grelmicro.cache import TTLCache
from grelmicro.idempotency import Idempotency

idem = Idempotency("charge", ttl=3600, cache=TTLCache(ttl=3600))
```

Responses serialize through the cache serializers. The default is `JsonSerializer`. Pass `serializer=PydanticSerializer(Model)` or `serializer=PickleSerializer()` to store richer types.

## The block form

`idem(key)` opens an async context manager. The yielded operation carries `replayed`, `result()`, and `store(...)`.

```python
async with idem(key) as op:
    if op.replayed:
        return op.result()
    response = await do_work()
    op.store(response)
    return response
```

On a first execution, `replayed` is `False`. Call `op.store(response)` to persist the response. On a replay, `replayed` is `True` and `op.result()` returns the stored value, typed as the stored type so the replay branch returns it without a cast. Calling `op.result()` on a first execution raises `IdempotencyStateError`, so guard it with `if op.replayed:`.

Exiting the block without calling `op.store(...)` on a first execution stores nothing. The operation opted out and a later call with the same key executes fresh.

## One-call form

`idem.run(key, factory)` owns the block. It runs the factory once, stores the response, and replays it on a repeated key. The factory can be sync or async. It mirrors `TTLCache.get_or_set`.

```python title="run.py"
--8<-- "idempotency/run.py"
```

The first call for a key runs the factory and stores its return value. A later call with the same key replays the stored value without running the factory. A failing factory stores nothing, so a later retry runs fresh. Pass `fingerprint=` to guard against a key reused with a different payload.

## Decorator

`@idempotent` derives the key from the call arguments and stores the return value. It mirrors `@cached`.

```python
from grelmicro.idempotency import idempotent


@idempotent(idem, key=lambda **kw: kw["idempotency_key"])
async def charge(*, amount: int, idempotency_key: str) -> dict:
    return await do_charge(amount)
```

The first call for a key runs the function and stores its return value. A later call with the same key replays the stored value without running the function.

## Single-flight duplicates

A duplicate that arrives while the first execution is still in flight waits and receives the stored response. It folds across replicas when a Coordination lock backend is configured, and in-process otherwise.

```python
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[Cache(redis), Coordination(redis)])
```

With a lock backend, two replicas that receive the same key at the same time run the work once and both return the same response.

### Bounding the wait

The wait is unbounded by default. Pass `wait_timeout=` to bound it, on the block or on `run()`. Past it the wait raises `IdempotencyWaitTimeoutError`, which subclasses `TimeoutError`.

```python
from grelmicro.idempotency import IdempotencyWaitTimeoutError

try:
    async with idem(key, wait_timeout=5) as op:
        ...
except IdempotencyWaitTimeoutError:
    ...  # an execution for this key is still in flight
```

[IdempotencyMiddleware](#http-middleware) bounds it at ten seconds and answers `409`.

## Payload fingerprint

Pass `fingerprint=` to guard against a key reused with a different payload. The fingerprint is a string the caller derives from the request body. It is stored on the first execution. A replay with a different fingerprint raises `IdempotencyConflictError`, because the same key with a different payload is a client bug.

```python
import hashlib

fingerprint = hashlib.sha256(raw_body).hexdigest()

async with idem(key, fingerprint=fingerprint) as op:
    ...
```

When no fingerprint is given, no check runs.

## What is and is not guaranteed

Guaranteed:

- A repeated key within `ttl` replays the stored response without running the operation again.
- An exception in the block or the decorated function stores nothing. A later retry with the same key executes fresh.
- A duplicate arriving mid-flight waits and replays the stored response.

Not guaranteed:

- A key replays only within `ttl`. After it elapses, the same key executes fresh.
- The work itself is not made atomic. Idempotency replays the response. Pair it with a transaction when the side effect must also be once-only.

## Configuration

Build with keyword arguments and tune `ttl` in deployment. Set
`GREL_IDEMPOTENCY_{NAME}_TTL` to change it without code changes (the default
instance drops the name segment and reads `GREL_IDEMPOTENCY_TTL`). The instance
reconfigures live from a mounted ConfigMap. See
[Live reconfiguration](architecture/reconfigure.md).

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition,
    see [Declarative configuration](advanced/config.md).
