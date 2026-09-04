# Idempotency Middleware

The [quick start](../idempotency/index.md#quick-start) reads the key and opens a block in every handler that needs one. `IdempotentRequests()` does it once, for the whole app.

```python title="middleware.py"
--8<-- "idempotency/middleware.py"
```

Handlers stay as they are. There is no key parameter to thread through and no block to open.

Register `IdempotentRequests()` and `micro.install(app)` adds the middleware and describes it in the OpenAPI schema. The bare form keeps a response for a day. `ttl=` says how long, and `namespace=` and `cache=` say where.

The middleware itself is pure ASGI and runs in front of any ASGI app. Add it by hand when the app never goes through `install`:

```python
from grelmicro.http import IdempotencyMiddleware
from grelmicro.idempotency import Idempotency

app.add_middleware(
    IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
)
```

Either way it resolves its `Cache` through the grelmicro request scope, and `install` keeps that scope outside every other middleware.

## Which endpoints it applies to

Endpoints are grouped by the router they sit on, so selection follows the
router:

```python
payments = APIRouter(prefix="/payments")
app.include_router(payments)

IdempotentRequests(include=("/payments/*",))
```

Three filters on the component, and a fourth door that skips it entirely.

```python
IdempotentRequests(
    methods=("POST", "PATCH"),  # which verbs take a key
    include=("/payments/*",),  # which paths to act on, empty means all
    exclude=("/payments/webhook",),  # which to leave alone, and it wins
)
```

`methods` is the first cut: every other verb passes straight through. A path
matches exactly, unless the pattern ends with `*`, which matches as a prefix.
It is the same word and the same matching on every grelmicro middleware.

The OpenAPI schema is annotated by the same patterns, matched against the route
as it is declared. Keep a pattern above any path parameter, so `/tenants/*`
rather than `/tenants/acme/*` on a `/tenants/{tenant}/orders` route, and what
the schema publishes is what the middleware covers. A pattern that matches no
declared route, which is what a mount prefix or a `root_path` leads to, has
every operation of the covered methods described rather than none.

A request without the header passes through anyway, so a route that never
sends one is already unaffected.

**For one route rather than the app**, skip the middleware and use the pattern
directly. The [block form](../idempotency/index.md#quick-start) and the
`@idempotent` decorator make one operation idempotent, with no middleware
registered at all. The difference is what is replayed: the middleware replays
the whole HTTP response, the block replays the value your function returned.

## What each refusal answers

The middleware writes these itself, before the handler runs, so they are the
same on FastAPI, Starlette and Litestar, byte for byte:

| The client did | Status | Why that one |
|---|---|---|
| Sent no key on a route with `require_key` | `400` | The request is malformed for this endpoint. |
| Sent a key over 255 characters | `400` | Longer than the schema publishes. |
| Sent a key holding a byte outside printable ASCII | `400` | A control byte or a high byte reaches the cache as a key and travels through proxies that may rewrite it. |
| Reused a key with a different body, under `fingerprint_body` | `422` | See [Conflicting payloads](#conflicting-payloads). |
| Sent a body over `max_body_size`, under `fingerprint_body` | `413` | Larger than the service reads. |
| Retried while the first request is still running | `409` | See [Duplicates in flight](#duplicates-in-flight). |

Every one is a `4xx`, because every one is something the client can fix. A
backend that is down is the service's fault and stays a `5xx`.

## It runs behind your authentication

`micro.install(app)` puts grelmicro's middleware **innermost**, behind every
middleware the app added itself, whichever order the two were wired in.

That placement is not a preference. A middleware that answers a request on its
own, such as an idempotent replay, must never be the reason a request skipped
the authentication in front of your handlers. Innermost means a replay is
served only after your own middleware has run and let the request through, so a
caller who learns another caller's key is turned away exactly as they would be
on a first request.

`GrelmicroMiddleware` stays outermost, because it only binds a context variable
and changes nothing a client can see.

!!! warning "Litestar builds its stack at construction"
    There, `install` can only wrap the whole app, which puts the middleware
    outside the app's own. grelmicro warns with
    [`middleware-placement`](../diagnostics.md#middleware-placement) and names
    the fix: pass it to `Litestar(middleware=[...])`, after your authentication,
    and `install` then leaves it alone.

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

A request without the `Idempotency-Key` header passes straight through. Adding the middleware changes nothing until a client opts in.

### The two header names

`Idempotency-Key` is the request header the [Idempotency-Key header draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) registers.

`Idempotent-Replayed` is not standard. The draft names no response header for a replay, so every API picked its own. `Idempotent-Replayed: true` is the one most of them answer with, including Stripe and Increase, which is why it is the default here. Others read `Idempotency-Replayed`, and a few carry a vendor prefix.

Rename either one when your clients already read another:

```python
IdempotentRequests(
    key_header="X-Idempotency-Key",
    replay_header="X-Idempotent-Replayed",
)
```

Both take an HTTP field name. A name holding a space, a colon, a newline, or a non-ASCII character is refused when the component is built, rather than reaching the wire as a broken header. `replay_header` also refuses a name that directs the client, such as `Content-Type`, `Location`, or `Content-Length`, because the marker would take its place. `key_header` refuses a name every request already carries, such as `Content-Type` or `Authorization`, because callers sharing that value would share one stored response.

Give the marker a name of its own. A handler that sets the replay header itself loses that header on a replay, and the replacement is logged once at warning level.

Both names reach the OpenAPI schema, so a client generated from it reads the name your service picked rather than the default.

## Errors replay too

Every response the app returns is stored, `4xx` and `5xx` included. A handler that writes to the database and then fails on the way out leaves a stored failure, so the retry gets that instead of running the write again.

Store only what you want replayed. A transient `503` stored for the whole `ttl` keeps answering `503` long after the dependency recovers, so exclude the statuses that mean "try again later":

```python
IdempotentRequests(
    skip=lambda response: response["status"] in (429, 502, 503, 504),
)
```

A handler that raises an unhandled exception is different. The middleware sits under whatever turns an exception into a response, on every framework, so nothing is stored and a retry runs fresh rather than replaying a `500` for the whole window. A raised `HTTPException` is not an unhandled exception. The framework turns it into a response the handler chose, so it is stored and replayed like any other.

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
IdempotentRequests(skip=lambda response: response["status"] == 202)
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


    IdempotentRequests(key_maker=tenant_key)
    ```

    Three things carry the isolation here. The identity comes from authentication rather than from the request. The separator is one an identity cannot contain, so `a` plus `b|c` cannot collide with `a|b` plus `c`. And a missing identity raises instead of returning a partial key.

    That last one is the general rule: **a key that is partly missing does not fail, it merges.** Callers whose key lost the same component land in one entry and replay each other, while the request still answers normally.

    `scope["user"]` is set by an authentication middleware, and reading it requires that middleware to run **outside** this one, which means adding it **after**. The same applies to anything else the key reads from the scope, including `ClientAddressMiddleware`:

    ```python
    micro = Grelmicro(uses=[redis, IdempotentRequests(key_maker=tenant_key)])
    micro.install(app)
    app.add_middleware(AuthenticationMiddleware, backend=...)
    ```

    Get that backwards and the value is not set yet. `IdempotencyMiddleware` refuses a key carrying an unresolved `None` rather than storing under it, so the mistake surfaces on the first request instead of quietly merging callers.

!!! warning "A client address is not a tenant identity"
    `ClientAddressMiddleware` resolves who connected, not who they are. Carrier-grade NAT puts many subscribers behind one address, so they would share an idempotency entry, and a caller moving between networks changes address mid-retry and loses the replay. Use it to rate limit, not to separate tenants.

`key_maker` receives the ASGI scope and the client's key, and returns the whole stored key. It mirrors [`key_maker` on `@cached`](../cache/cached.md#custom-keys).

## Duplicates in flight

A duplicate that arrives while the first execution is still running waits for it, then replays its response.

The wait folds duplicates across replicas when a `Coordination` lock backend is configured, and in-process otherwise. See [Single-flight duplicates](../idempotency/index.md#single-flight-duplicates).

The wait is bounded by `wait_timeout`, ten seconds by default:

```http
HTTP/1.1 409 Conflict
content-type: application/problem+json
retry-after: 1

{
  "type": "https://grelmicro.grel.info/http/errors/#idempotency-in-flight",
  "title": "Idempotent request in flight",
  "status": 409,
  "detail": "A request with this Idempotency-Key is still running. Retry after the delay in the Retry-After header to read its response.",
  "instance": "/charge",
  "retry_after": 1.0
}
```

Past the timeout the duplicate receives that instead of holding the connection open.

Every response the middleware writes itself follows the [error format](errors.md) the app registered, the `400`, `409`, `413`, and `422` alike, so a client reads the same shape here as from a rejection raised in a handler. The body below is RFC 9457, the default.

## Conflicting payloads

Pass `fingerprint_body=True` to hash the request body and store the hash with the response. A key reused with a different body then gets `422 Unprocessable Content` instead of a wrong replay, matching the [Idempotency-Key header draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/).

```python
IdempotentRequests(fingerprint_body=True)
```

Fingerprinting buffers the request body before the handler runs, so it is off by default. A request body over `max_body_size` is answered with `413`.

The draft says `422` here and Stripe answers `400`, so this is the one status the middleware lets you pick:

```python
IdempotentRequests(fingerprint_body=True, reused_status=400)
```

The body does not change, so a client branching on the `type` identifier reads the same thing either way, and the OpenAPI schema publishes whichever status you chose. The other two statuses are not configurable, because the draft, Stripe, and every implementation in between already agree on them: `400` for a missing or malformed key, `409` for a duplicate arriving while the first request is still running.

The block form raises `IdempotencyConflictError` to your handler instead, which [Error Responses](errors.md) renders with the draft's `422`.

## OpenAPI

The middleware runs outside the routing layer, so nothing it does reaches the generated schema. A client built from that schema never learns the header exists, and Swagger offers no field for it. `micro.install(app)` writes it there, so a registered component needs nothing else: every operation the middleware covers gains the `Idempotency-Key` field and the responses the middleware itself returns. Pass `openapi=False` to leave the schema alone:

```python
IdempotentRequests(openapi=False)
```

A middleware added by hand is documented with `document_idempotency`:

```python
from grelmicro.http import IdempotencyMiddleware
from grelmicro.integrations.fastapi import document_idempotency

micro.install(app)
app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
document_idempotency(app)
```

Only FastAPI builds an OpenAPI schema, so every other framework ignores this.

Every operation the middleware covers gains the `Idempotency-Key` header parameter and the responses the middleware itself returns. Call it any time after `add_middleware`, and routes added afterwards are covered too.

An operation that already declares the header keeps its own declaration. The `422` that FastAPI generates for request validation keeps its schema and gains the idempotency case in its description and the problem media type alongside it, so neither is lost.

Each response the middleware adds points at a `ProblemDetail` component, so a generated client knows the body it will get.

!!! note "Mounted sub-applications"
    A mounted sub-application builds its own schema. Call `document_idempotency` on it as well.

## Background tasks

A background task runs after the response is sent, so the response is stored and replayable while the task is still working. A replay can land before the original request's background work finishes. Put anything a retry must not repeat in the handler, not in a background task.

## Parameters

`IdempotentRequests` and `IdempotencyMiddleware` take the same options, so a registered component and a hand-added middleware answer the same.

| Parameter | Default | Behaviour |
|---|---|---|
| `ttl` | one day | Seconds a stored response replays for. Component only. |
| `namespace` | `"http"` | Namespace the stored keys sit under. Component only. |
| `cache` | the registered `Cache` | The `TTLCache` responses are stored in. Component only. |
| `idempotency` | required on the middleware | The `Idempotency` it stores through. The component builds one from `ttl`, `namespace` and `cache`. |
| `key_header` | `"Idempotency-Key"` | Request header carrying the key. Up to 255 printable ASCII characters, such as a UUID. |
| `replay_header` | `"Idempotent-Replayed"` | Response header marking a replay. No standard names one, so pick what your clients read. |
| `methods` | `("POST",)` | Methods that take a key. Every other method passes through. |
| `key_maker` | `None` | Build the stored key from the scope and the client key. Set it in any multi-tenant app. |
| `skip` | `None` | Predicate over the finished response. Return `True` to not store it. |
| `require_key` | `False` | Answer `400` when a matched method arrives without the header. |
| `fingerprint_body` | `False` | Hash the request body and answer `422` on a reused key with a different body. |
| `max_body_size` | `1048576` | Largest body held in memory, in bytes. Caps the stored response, and the fingerprinted request. |
| `wait_timeout` | `10.0` | Seconds a duplicate waits for an execution in flight before `409`. |
| `include` | `()` | Paths the middleware acts on. Empty means every path. Exact match unless the pattern ends with `*`. |
| `exclude` | `()` | Paths the middleware leaves alone, whatever `include` says. |
| `reused_status` | `422` | Status for a key reused with a different payload. `400` matches Stripe. |
| `openapi` | `True` | Describe both headers and the middleware responses in the OpenAPI schema. Only this component's rules, so a second set can stay unpublished. Component only, and only FastAPI builds one. |
| `name` | `"default"` | Registration name, for a second set of rules on one app. Component only. |
