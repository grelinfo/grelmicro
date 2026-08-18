# HTTP Middleware

The [quick start](index.md#quick-start) reads the key and opens a block in every handler that needs one. `IdempotencyMiddleware` does it once, for the whole app.

```python title="middleware.py"
--8<-- "idempotency/middleware.py"
```

Handlers stay as they are. There is no key parameter to thread through and no block to open.

Add the middleware before or after `micro.install(app)`. It resolves its `Cache` through the grelmicro request scope, and `install` keeps that scope outside every other middleware.

## What a client sees

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

## Errors replay too

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

## What is never stored

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

## Stored responses sit at rest

A stored response is the whole response, headers and body, held in the cache
backend for `ttl`. A body carrying a token, a signed URL, or personal data is at
rest there for that long, on whatever backend the `Cache` component points at.

`ttl` is therefore a retention decision, not only a replay window. Set it to the
shortest window that still catches the retries you care about, rather than the
longest one the cache will tolerate. Use `skip` to keep a response out of the
cache entirely when its body should never be written down.

Responses carrying `Set-Cookie` are already never stored, because replaying one
hands a caller another caller's session.

## Custom rules with `skip`

`skip` receives the finished response and returns `True` to leave it unstored. It mirrors [`skip` on `@cached`](../cache/cached.md#decorator-parameters).

```python
app.add_middleware(
    IdempotencyMiddleware,
    idempotency=Idempotency("http"),
    skip=lambda response: response["status"] == 202,
)
```

Use it for a response that is technically replayable but should not be. The four rules above run first, so `skip` never has to re-check them.

## Keys are scoped per route

The stored key combines the method, the path, the query string, and the header value. The same client key on `POST /charge` and `POST /refund` stores two entries, so one route never replays another one's response.

!!! warning "Set `key_maker` in a multi-tenant app"
    Without it, any client that learns another client's key replays their response, body included. Fold the caller identity into the key.

    Fold in an **authenticated** identity. Anything the client sends is chosen by the client, so a key built from a raw header lets a caller name the tenant whose entry they read.

    ```python
    SEP = "\x1f"  # not a byte an identity can contain


    def tenant_key(scope, key):
        user = scope.get("user")
        if user is None or not user.is_authenticated:
            raise PermissionError("idempotency needs an authenticated caller")
        return SEP.join((str(user.tenant_id), scope["path"], key))


    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        key_maker=tenant_key,
    )
    ```

    Three things carry the isolation here. The identity comes from authentication rather than from the request. The separator is one an identity cannot contain, so `a` plus `b|c` cannot collide with `a|b` plus `c`. And a missing identity raises instead of returning a partial key.

    That last one is the general rule: **a key that is partly missing does not fail, it merges.** Callers whose key lost the same component land in one entry and replay each other, while the request still answers normally.

    `scope["user"]` is set by an authentication middleware, and reading it requires that middleware to run **outside** this one, which means adding it **after**. The same applies to anything else the key reads from the scope, including `ClientAddressMiddleware`:

    ```python
    micro.install(app)
    app.add_middleware(IdempotencyMiddleware, idempotency=idem, key_maker=tenant_key)
    app.add_middleware(AuthenticationMiddleware, backend=...)
    ```

    Get that backwards and the value is not set yet. `IdempotencyMiddleware` refuses a key carrying an unresolved `None` rather than storing under it, so the mistake surfaces on the first request instead of quietly merging callers.

!!! warning "A client address is not a tenant identity"
    `ClientAddressMiddleware` resolves who connected, not who they are. Carrier-grade NAT puts many subscribers behind one address, so they would share an idempotency entry, and a caller moving between networks changes address mid-retry and loses the replay. Use it to rate limit, not to separate tenants.

`key_maker` receives the ASGI scope and the client's key, and returns the whole stored key. It mirrors [`key_maker` on `@cached`](../cache/cached.md#custom-keys).

## Duplicates in flight

A duplicate that arrives while the first execution is still running waits for it, then replays its response.

The wait folds duplicates across replicas when a `Coordination` lock backend is configured, and in-process otherwise. See [Single-flight duplicates](index.md#single-flight-duplicates).

The wait is bounded by `wait_timeout`, ten seconds by default:

```http
HTTP/1.1 409 Conflict
content-type: application/problem+json
retry-after: 1

{
  "type": "https://grelmicro.grel.info/http/problems/#idempotency-in-flight",
  "title": "Idempotent request in flight",
  "status": 409,
  "detail": "A request with this Idempotency-Key is still running. Retry after the delay in the Retry-After header to read its response.",
  "instance": "/charge",
  "retry_after": 1.0
}
```

Past the timeout the duplicate receives that instead of holding the connection open.

Every response the middleware writes itself is a [problem detail](../http/problems.md), the `400`, `409`, `413`, and `422` alike. A client branches on `type` and gets the same shape here as from a rejection raised in a handler.

## Conflicting payloads

Pass `fingerprint_body=True` to hash the request body and store the hash with the response. A key reused with a different body then gets `422 Unprocessable Content` instead of a wrong replay, matching the [Idempotency-Key header draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/).

```python
app.add_middleware(
    IdempotencyMiddleware,
    idempotency=Idempotency("http"),
    fingerprint_body=True,
)
```

Fingerprinting buffers the request body before the handler runs, so it is off by default. A request body over `max_body_size` is answered with `413`.

## OpenAPI

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

Every operation the middleware covers gains the `Idempotency-Key` header parameter and the responses the middleware itself returns. Call it after `micro.install(app)`, which is where the error format is registered, and routes added afterwards are covered too.

An operation that already declares the header keeps its own declaration. The `422` that FastAPI generates for request validation keeps its schema and gains the idempotency case in its description and the problem media type alongside it, so neither is lost.

Each response the middleware adds points at a `ProblemDetail` component, so a generated client knows the body it will get.

!!! note "Mounted sub-applications"
    A mounted sub-application builds its own schema. Call `document_idempotency` on it as well.

## Background tasks

A background task runs after the response is sent, so the response is stored and replayable while the task is still working. A replay can land before the original request's background work finishes. Put anything a retry must not repeat in the handler, not in a background task.

## Middleware parameters

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
