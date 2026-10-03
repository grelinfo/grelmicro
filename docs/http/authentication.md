# Authentication

A service checks the token a caller presents before any handler runs. Register
one component and every request is authenticated: the bearer token is read,
verified, and the caller handed to the route.

```python
--8<-- "http/authentication.py"
```

`GET /orders` needs a valid token. `DELETE /orders/{order_id}` needs one that
grants `orders:write`. `GET /catalog` needs none.

The words this page uses:

- **Bearer token**: the token a caller sends in `Authorization: Bearer <token>`.
  Here it is a JWT that your identity provider signed.
- **Claims**: the fields inside the token, such as `sub` (who the caller is),
  `exp` (when it expires), `aud` (the audience: which service it is meant for)
  and `jti` (a unique id).
- **Scope**: a permission the token grants, such as `orders:write`.
- **Principal**: the verified caller, which handlers receive as
  `CurrentPrincipal`.

The [JWT guide](../security/jwt.md) explains how a token is verified.

## What is authenticated

Everything, unless it says otherwise. There is no `include`, so a route added
tomorrow is authenticated the day it lands, and a mistyped pattern can never
leave an endpoint public without a word.

Two things say otherwise:

- `exclude` names the paths never authenticated, such as health probes. A token
  sent to them is not read.
  A pattern is an exact path, or a prefix ending in `/*`. A pattern matching
  every path, `*` or `/*`, is refused. So is a prefix that ends inside a
  segment: `/public*` would also serve `/publicity` without a credential.
  Write `/public/*` for everything under it, or `/public` for that path alone.
- `Anonymous()` declared on a route makes the credential optional there. It applies
  per method, so a public read keeps the writes on the same path
  authenticated. It is read off the route the router dispatches the request
  to, so a declaration never opens a route it was not written on, whatever
  URL both could answer.

On an `Anonymous()` route, a request sending no token is served with a caller
that is not authenticated. A bearer token that is sent is verified as on any
other route: a valid one is the caller, and one that does not verify is answered
`401`. On an excluded path the caller is never authenticated, token or not.

A route that requires a caller, through `Authenticated`, `CurrentPrincipal` or
`Claims`, is refused by `micro.install(app)` when it declares `Anonymous()` or
sits in `exclude`. It could never serve the requests it was written for. Read
`OptionalPrincipal` on a public route instead.

HTTP requests and websocket handshakes are both covered.

### Decided at the route, on Starlette

On Starlette, `micro.install(app)` puts a gate in front of every route and
every `HTTPEndpoint` method, and in front of every mount whose app is not a
Starlette router. A request without a credential is refused before routing,
unless its path is in `exclude` or a mounted FastAPI app declares an
`Anonymous()` route. A request whose token verified is routed, and the gate
of the route it reaches checks its scopes.

- A URL no route answers gets the same `401` as a protected route, and the
  `401` names no scope.
- A mount whose app is not a Starlette router, such as `StaticFiles`, a
  mounted app or a mount with middleware, is also one protected route, and the
  routes it holds are gated too. A router `default` of your own is gated.
- A mounted FastAPI app is gated route by route. While it holds an
  `Anonymous()` route, its mount lets a request without a credential
  through, and each of its routes decides.
- A route added later, through the app or any of its routers, is gated as it
  lands. A route list, a mount's app or a router's default assigned once the
  app serves is gated before the next request goes through it. An app whose
  router is replaced after `install` refuses to start.
- A path in `exclude` stays excluded only as far as the route it is routed to.
  A mounted app's middleware that rewrites `/files/../admin` to `/admin` meets
  the gate of `/admin`, which refuses it. So does a request whose middleware
  rebuilds the scope on the way.
- A route shared by several apps is gated with each app's own `exclude` and
  error format. An app without `AuthenticatedRequests` gets `401` on it.

### Decided at the route, on FastAPI

On FastAPI, the gate sits where FastAPI hands a request to the route it
matched, before the route reads the request body. A route declaring
`Anonymous()` serves a caller with no credential, and the scopes of every
`Authenticated` around it, on the route or on a router it was included with,
are checked before the handler runs.

- A request without a credential is routed when a route of the app declares
  `Anonymous()`, and refused before routing otherwise. A URL no route
  answers, a method no route serves and a trailing slash redirect get the
  same `401` as a protected route.
- A request a route refuses is answered before its body is read, so an
  invalid body on a protected route gets `401`, not `422`.
- A router included twice is gated under each include with what that include
  adds. `include_router(router, dependencies=[Anonymous()])` makes its routes
  public under that include alone. Websocket routes, Starlette routes, mounts
  and frontend routes are gated too.
- A mount holding no `Anonymous()` route refuses a request without a
  credential before anything under it runs, its middleware included. A
  mounted app holding one runs its own middleware, and each of its routes
  decides. That middleware runs for a request without a credential too. Its
  own CORS middleware answers a preflight.
- A FastAPI app mounted under a Starlette app is gated route by route.
- A route added later, to the app or to a router it includes, is gated as it
  lands. A route removed or changed in place is served as it is now, and a
  mount whose last `Anonymous()` route is gone refuses at its door again.

### Decided at the handler, on Litestar

On Litestar, the gate sits where Litestar's router hands a request to the
handler it matched, per method. A handler declaring `Anonymous()` serves a
caller with no credential, and `Authenticated(scopes=[...])` guards name the
scopes the gate checks before the handler runs.

- A request without a credential is routed when a handler of the app declares
  `Anonymous()`, and refused before routing otherwise. A URL no handler answers
  and a method no handler serves get the same `401` as a protected route.
- `Anonymous()` on one method keeps the route's other methods authenticated,
  and so is the `OPTIONS` Litestar adds to a route. A CORS preflight is answered
  by Litestar's CORS middleware before authentication.
- An ASGI mount is one route: its app, and any middleware in it, never runs for
  a request its handler refuses.
- A handler registered later is gated as it lands.
- A middleware declared in `Litestar(middleware=[...])` runs behind the
  router, and checks the handler it serves the same way. The rate limit, the
  response cache, idempotency and conditional requests then run behind it,
  once the handler's declaration admitted the request. It is part of each
  handler's own stack, so a URL no handler answers never reaches it, and
  Litestar answers it `404`. Let `micro.install(app)` place it for that URL to
  get the `401`.

### Rate limits, caching and idempotency run at the route

On Starlette, FastAPI and Litestar, `RateLimitedRequests`, `CachedResponses`,
`IdempotentRequests` and `ConditionalRequests` run once the gate of the route
admitted the request, around its handler. A request a route refuses spends no
rate-limit budget, costs no cache lookup and stores nothing under its
`Idempotency-Key`. A request no route answers reaches none of them. Their
`include=` and `exclude=` still name paths from the app's root, under a mount
too.

## Reading the caller

The verified caller is put in `request.user` and `request.auth`, which is where
Starlette, FastAPI and Litestar look.

| | FastAPI | Litestar | Starlette |
|---|---|---|---|
| read the caller | `principal: CurrentPrincipal` | `request.user` | `request.user` |
| the caller, if any | `principal: OptionalPrincipal` | `request.user` | `request.user` |
| JWT claims, typed | `claims: Claims` | `request.user` | `request.user` |
| the token that verified | `token: CurrentToken` | `current_token(request)` | `current_token(request)` |
| require scopes | `dependencies=[Authenticated(scopes=[...])]` | `guards=[Authenticated(scopes=[...])]` | `@Authenticated(scopes=[...])` |
| public route | `dependencies=[Anonymous()]` | `opt=Anonymous()` | `exclude=` |

On Litestar, `Anonymous()` is `{"exclude_from_auth": True}`, the key Litestar's
own authentication middleware reads, so a handler written for that one is
public here too.

`CurrentPrincipal` is a `Principal`: `subject`, `issuer`, `scopes` and
`claims`, whatever proved who the caller is. Key a caller by `issuer` and
`subject` together, because a subject is unique only within the issuer that
assigned it.

`CurrentToken` is the token itself, once it verified, for a handler that acts
for the caller, such as one [exchanging it](../security/tokens.md#call-an-api-for-the-user)
for a token issued to another API.

`Authenticated(scopes=...)` requires every scope it names. A caller lacking one
is answered `403`, never `401`. On Starlette it decorates a function, an
`HTTPEndpoint` method or a websocket endpoint. Starlette's own `@requires` works
too, but answers a plain `403` with no challenge. On FastAPI it is built on `fastapi.Security`,
so a handler can take the caller from it too, and a router and its route each
apply their own.

## What a refused caller is told

Each refusal is rendered by the registered [`ErrorResponses`](errors.md), with
the `WWW-Authenticate` challenge
[RFC 6750](https://www.rfc-editor.org/rfc/rfc6750#section-3) gives it:

| Case | Status | `type` anchor | Challenge |
|---|---|---|---|
| No credential, or another scheme such as `Basic` | `401` | [`authentication-required`](errors.md#authentication-required) | `Bearer`, naming no scope |
| A token that does not verify | `401` | [`token-rejected`](errors.md#token-rejected), with `reason` | `Bearer error="invalid_token"` |
| More than one credential | `400` | [`ambiguous-credentials`](errors.md#ambiguous-credentials) | `Bearer error="invalid_request"` |
| A missing scope | `403` | [`insufficient-scope`](errors.md#insufficient-scope) | `Bearer error="insufficient_scope"` with `scope=` |
| A caller `bans` refuses | `429` | [`client-banned`](errors.md#client-banned) | none, `Retry-After` instead |
| Keys that have not loaded | `503` | [`signing-keys-unavailable`](errors.md#signing-keys-unavailable) | none |

No message quotes the token, and `error_description` is never sent, so no claim
value reaches a header.

A refused websocket gets the same `401` and challenge on a server that supports
the denial response extension. On one that does not, the handshake is closed
before it completes, which the server answers `403` with no headers.

## Checking the caller after the token verifies

A token stays valid until its `exp`, even after the user signs out or an
administrator revokes it. `check=` runs after the token verifies and before the
app sees the request:

```python
--8<-- "http/authentication_check.py"
```

This refuses a token whose `jti` was revoked, and every token issued before the
user signed out everywhere. Store the sign-out time in whole seconds, such as
`int(time.time())`, and compare whole seconds of `iat` too, which may carry a
fraction. A token issued in the sign-out's second is refused as well, since
nothing tells it apart from one issued just before: the client gets a `401` and
fetches a new token.

- Return the caller to serve the request, or `None` to refuse it. A refused
  caller is answered `401` [`token-rejected`](errors.md#token-rejected) with
  reason `revoked`, and `bans` never counts it.
- It runs on every request carrying a verified token: one the verifier answered
  from its cache, one sent to an `Anonymous()` route, and a websocket handshake.
  A request without a token, or on an excluded path, never reaches it.
- Its answer is never cached, so a revocation is refused on the very next
  request. A check that keeps a cache of its own never keeps an entry past the
  token's `exp`.
- It receives the request's ASGI scope, to read a header or the address
  [`ClientAddressMiddleware`](../security/clientip.md) resolved.
- An error it raises never serves the request. One `ErrorResponses` knows is
  rendered, such as the `DeadlineExceededError` of a
  [`Timeout`](../resilience/timeout.md) around the store. Anything else is a
  `500`, including your framework's `HTTPException`, which is only answered
  inside a route. Refuse with `None`.
- It may be a plain function too, for a check that needs no I/O.

Return your own object to hand every route the user behind the token:

```python
@dataclass(frozen=True)
class User:
    subject: str | None
    issuer: str | None
    scopes: frozenset[str]
    claims: Mapping[str, Any]
    name: str
    is_authenticated: bool = True


async def load_user(caller: Principal, scope: Scope) -> User | None:
    record = await users.find(caller.issuer, caller.subject)
    if record is None or not record.active:
        return None
    return User(caller.subject, caller.issuer, caller.scopes, caller.claims, record.name)
```

- `request.user`, `request.auth` and `CurrentPrincipal` are the object you
  return. It must be an authenticated `Principal`, or the request is a `500`.
- `Authenticated(scopes=...)` reads its `scopes`, so carry over the ones the
  token grants.
- `Claims` reads only a `JWTClaims`. Read `CurrentPrincipal` once `check`
  returns your own object.

A decision that depends on the route, such as whether the caller owns the
order, stays in the route and answers `403`.

## Telling a client where to get a token

```python
AuthenticatedRequests(
    JWTVerifier.discover("https://auth.example.com/", audience="orders-api"),
    resource="https://api.example.com/orders",
)
```

A refused client learns that it needs a bearer token, but not where to get one.
With `resource=`, the service publishes its
[protected resource metadata](https://www.rfc-editor.org/rfc/rfc9728) and every
challenge points at it. A client that discovers authorization at runtime, such
as an [MCP](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
client, then needs nothing but the service's URL.

`GET /.well-known/oauth-protected-resource/orders` answers:

```json
{
  "resource": "https://api.example.com/orders",
  "authorization_servers": ["https://auth.example.com/"],
  "bearer_methods_supported": ["header"]
}
```

Every `Bearer` challenge carries `resource_metadata`, whichever refusal it is,
and whether the middleware or a route raised it:

```
WWW-Authenticate: Bearer error="insufficient_scope", scope="orders:write", resource_metadata="https://api.example.com/.well-known/oauth-protected-resource/orders"
```

- `resource` is the URL clients call the service at, as they see it behind any
  proxy. It must be `https`, with no fragment, and no brace in its path, which a
  router reads as a path parameter. A client refuses a document whose
  `resource` differs from the URL it asked about, so nothing in it is read from
  the request.
- The document is served at `/.well-known/oauth-protected-resource` followed by
  the path of `resource`. Route that path to the service. The path is the
  middleware's: it answers every request there, so `micro.install(app)` refuses
  a route the app declares at it.
- `authorization_servers` lists the verifier's issuers. A verifier that names
  none, such as one of your own, needs `authorization_servers=`.
- `scopes=` lists the scopes a client may ask for. Left out, none are listed:
  the document is public, and each challenge already names the scopes its route
  needs.
- The document is never authenticated, and any origin can read it.
- On Litestar, a middleware declared in `Litestar(middleware=[...])` runs behind
  the router, so `micro.install(app)` adds a route at that path for it. Built by
  hand without `install`, it warns with
  [`middleware-placement`](../diagnostics.md#middleware-placement).

## Keys

The verifier is opened with the app, so its keys load before the first request
and stay fresh in the background while it serves. A provider that cannot be
reached at startup does not stop the app: requests are answered `503` until the
keys arrive.

A token naming a key the verifier does not hold waits for one refresh and is
verified again, so the first request after a rotation is served. The verifier
fetches at most once per `retry_interval`, so a caller inventing key ids costs
one fetch rather than one per request.

The [JWT](../security/jwt.md) guide covers the verifier itself: keys you hold, a
JWKS URL, discovery from an issuer, and what gets checked.

The verifier does not have to be a `JWTVerifier`. Any object whose
`verify(token)` returns a `Principal` is accepted, and one returning an
awaitable is awaited, so a verifier that asks the authorization server about each
token fits too.

## Refusing a caller that keeps forging

```python
AuthenticatedRequests(
    verifier,
    bans=ClientBans(),
    trusted=TrustedProxies(["10.0.0.0/8"]),
)
```

A caller presenting tokens with a forged signature is banned for a while, and
answered `429` before its next token is verified. Only a forged signature or
algorithm counts, never an expired token or a key the provider is rotating.

`trusted=` is required with `bans`. A ban counted against an address the caller
can choose would refuse somebody else. Bans stay off unless `bans=` is given,
and [Bans are opt-in](../architecture/jwt.md#bans-are-opt-in) says why. Read
[Shedding a caller that keeps forging](../security/jwt.md#shedding-a-caller-that-keeps-forging)
for the thresholds.

## What each flood meets

There is nothing to guess. grelmicro verifies tokens and issues none, so no
password or login is exposed, and a signature cannot be forged by trying. A
flood can only cost the service work, and each kind meets its own guard:

| A flood of requests | Meets | Cost of each |
|---|---|---|
| Without a token | `401` before routing | almost nothing |
| With a malformed token | `401` before the signature is checked | under a microsecond |
| With a forged signature | `bans=`, `429` before verifying again | one lookup once banned |
| With an expired or misdirected token | `401` after verifying | about 12 microseconds |
| With a valid token, to URLs no route answers | [`flood=`](rate-limit.md#limiting-floods-before-routing) | one limiter check |
| With a valid token, to routes | [the route limits](rate-limit.md) | one limiter check |

On an app with a public route, a request without a token is routed to find
out whether that route serves it, so it spends `flood=` too.

`bans=` and `flood=` are both off unless given. A flood spread over many
addresses gets past anything counted per address, so it is for the ingress
in front of the service to absorb.

## Security events

Every refusal is written as a security event, so an operator sees a burst of
forged tokens and can tell an attack from a key rotation. There is nothing to
register. The records go to the `grelmicro.security.events` logger, the
counters to [`Metrics`](../metrics.md) when it is registered, and the refusal
to the current span when one is recording.

```json
{
  "level": "WARNING",
  "logger": "grelmicro.security.events",
  "msg": "GET /orders/{order_id} 401 signature",
  "otel.event.name": "grelmicro.authentication.refused",
  "event.kind": "event",
  "event.category": ["authentication"],
  "event.type": ["start"],
  "event.outcome": "failure",
  "event.action": "grelmicro.authentication.refused",
  "error.type": "signature",
  "http.request.method": "GET",
  "http.route": "/orders/{order_id}",
  "http.response.status_code": 401,
  "client.address": "203.0.113.9",
  "user_agent.original": "curl/8.4",
  "trace_id": "e3c64457486c59d0ba764839dc404da5",
  "span_id": "92495292bd9b0710",
  "grelmicro.security.suppressed": 12
}
```

- One record per refusal, at `WARNING`. A request that sent no credential is
  written at `INFO`, because a browser without a token is ordinary.
- `error.type` is one word from a fixed set: `authentication-required`,
  `ambiguous-credentials`, `insufficient-scope`, `client-banned`,
  `signing-keys-unavailable`, or the reason of a rejected token, such as
  `expired` or `signature`. It is the word the refusal's body carries.
- `http.route` is the route template, never the path. A route the app declares
  is named even when the middleware refused the request before routing.
- The categorization follows the
  [Elastic Common Schema](https://www.elastic.co/docs/reference/ecs/ecs-category-field-values-reference).
  A SIEM rule matching `event.category: authentication` and
  `event.outcome: failure` finds every refused credential. A missing scope is
  `event.category: web`, and its `event.action` is
  `grelmicro.authorization.refused`.
- `otel.event.name` names the event. The
  [OpenTelemetry logging instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/logging/logging.html)
  sends the record as a named event, tied to the request's span.
- Each kind of refusal from one address is written once per minute, so a
  request with no token never hides a forged one. The next record of that kind
  carries `grelmicro.security.suppressed`, the number held back. The counters
  stay exact. Callers that cannot be told apart, such as every caller behind a
  proxy [`ClientAddressMiddleware`](../security/clientip.md) does not trust,
  share one count, so a flood from behind it never floods the log.
- A request refused while its address is banned is counted, not written. The
  ban writes one record when it starts.
- No token, signature or signing secret reaches a record. A value read from
  the request is cut to 256 characters, with control characters escaped, so it
  can never forge a line of its own.
- HTTP requests and websocket handshakes are recorded the same way. A path in
  `exclude`, and an `Anonymous()` route sent no token, record nothing.

A ban that starts writes one record:

```json
{
  "level": "WARNING",
  "logger": "grelmicro.security.events",
  "msg": "client 203.0.113.9 banned for 300.0s after 10 failures",
  "otel.event.name": "grelmicro.client_bans.started",
  "event.category": ["intrusion_detection"],
  "event.type": ["denied"],
  "event.outcome": "success",
  "client.address": "203.0.113.9",
  "grelmicro.client_bans.name": "default",
  "grelmicro.client_bans.failures": 10,
  "grelmicro.client_bans.duration": 300.0,
  "grelmicro.client_bans.until": "2026-09-15T10:05:00+00:00"
}
```

### Naming the caller

```python
AuthenticatedRequests(verifier, enduser=True)
```

- The server span of every authenticated request carries `enduser.id`, the
  subject of the caller.
- A refusal carries `enduser.id` when the token's signature verified: an
  expired token, one issued for another audience, a missing scope, or a caller
  `check` refused. A forged token names nobody, whatever it claims.
- It is off by default. A subject can be personal data, such as an email
  address some providers use as `sub`, so writing it is a decision you make.
  [`AccessLog(enduser=True)`](../logging/access.md#who-called) writes it on the
  access record.
- No subject and no address ever becomes a metric attribute.

### Metrics

| Metric | Type | Attributes |
|---|---|---|
| `grelmicro.authentication.attempts` | counter | `grelmicro.outcome` (`success` or `refused`), with `error.type` and `http.route` when refused |
| `grelmicro.authorization.refusals` | counter | `error.type`, `http.route` |
| `grelmicro.client_bans.started` | counter | `grelmicro.client_bans.name` |
| `grelmicro.client_bans.active` | gauge | `grelmicro.client_bans.name` |

A request is one attempt. A caller refused for a missing scope already
authenticated, so it stays one `success` and counts on
`grelmicro.authorization.refusals`.

`grelmicro.client_bans.active` is read when metrics are collected, so a ban that
runs out leaves it without a request to report it.

### On the span

A refusal sets `grelmicro.authentication.refusal` on the current span. The span's
status and its `error.type` are left alone. The HTTP conventions leave a server
span's status unset for a `4xx`, so a refused token never reads as a server
error. The record above is the event, and carries the span's `trace_id` and
`span_id`.

## Where it sits

`micro.install(app)` places it ahead of every other middleware of ours that can
answer a request, whatever order it was registered in, so no cached or replayed
response reaches a caller that was never authenticated. It sits inside the
middleware your app adds itself, so CORS still answers a preflight.

The [response cache](cache.md) never stores an authenticated request, and an
[idempotent](idempotency.md) replay is skipped for one unless that component
builds its own key. An `Anonymous()` route stays cacheable and replayable.

## In the schema

On FastAPI and Litestar, the OpenAPI schema carries the security scheme and
requires it on every operation authentication covers, with the scopes its route
declares. Each of those operations lists the `401` it can answer, the `403`
where scopes are required, and the `429` when `bans` is set.

A route declaring `Anonymous()` lists the scheme as optional, beside an
alternative that requires nothing, with the `401` a token that does not verify
gets. An excluded path names no scheme at all.

A verifier built with `discover` publishes an `openIdConnect` scheme, so a
client can find the provider from the schema. Any other publishes a bearer
token. Pass `openapi=False` to leave the schema alone.

## Serving the API docs

The docs pages and the schema are routes like any other, so a browser opening
them needs a token too. Name them in `exclude` to publish them:

```python
AuthenticatedRequests(verifier, exclude=("/docs/*", "/redoc", "/openapi.json"))  # FastAPI
AuthenticatedRequests(verifier, exclude=("/schema/*",))  # Litestar
```

The schema lists every route and the scopes each one needs, so publish it only
where that is meant to be seen. [Testing](../testing.md#test-an-authenticated-app)
shows how to test the routes behind it.

## Reading it back

```bash
python -m grelmicro check app:micro --app app:app
```

```
Endpoints
  GET    /catalog            anonymous
  GET    /orders             authenticated
  DELETE /orders/{order_id}  authenticated orders:write
```

## Configuration

Nothing here is read from the environment, and nothing changes while the
service runs. Every setting decides what the service is protected by, so each
one changes with a deploy. The verifier's own trust settings, such as its
issuer and audience, can come from the deployment, as the
[JWT](../security/jwt.md#configure-from-the-deployment) guide shows.

For a middleware stack built by hand, `AuthenticatedRequestsMiddleware` takes
the same options. It serves public paths through `exclude` alone, because
only `micro.install(app)` reads `Anonymous()` off the routes.
