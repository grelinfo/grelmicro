# Authentication

A service checks the token a caller presents before any handler runs. Register
one component and every request is authenticated: the bearer token is read,
verified, and the caller handed to the route.

```python
--8<-- "http/authentication.py"
```

`GET /orders` needs a valid token. `DELETE /orders/{order_id}` needs one that
grants `orders:write`. `GET /catalog` needs none.

## What is authenticated

Everything, unless it says otherwise. There is no `include`, so a route added
tomorrow is authenticated the day it lands, and a mistyped pattern can never
leave an endpoint public without a word.

Two things say otherwise:

- `exclude` names the paths never authenticated, such as health probes. A token
  sent to them is not read.
  The patterns are the ones every other component takes: an exact path, or a
  prefix ending in `*`. A pattern matching every path, `*` or `/*`, is refused.
- `Anonymous()` declared on a route makes the credential optional there. It applies
  per method, so a public read keeps the writes on the same path
  authenticated. A URL another route could also answer stays authenticated,
  whichever route would serve it, so a declaration never opens a route it was
  not written on. A mount or a `Host` answers every path under it, except the
  public routes it holds. A route under a `Host` is served without one only
  where a request the host turns away is answered `404`.

On an `Anonymous()` route, a request sending no token is served with a caller
that is not authenticated. A bearer token that is sent is verified as on any
other route: a valid one is the caller, and one that does not verify is answered
`401`. On an excluded path the caller is never authenticated, token or not.

A route that requires a caller, through `Authenticated`, `CurrentPrincipal` or
`Claims`, is refused by `micro.install(app)` when it declares `Anonymous()` or
sits in `exclude`. It could never serve the requests it was written for. Read
`OptionalPrincipal` on a public route instead.

HTTP requests and websocket handshakes are both covered.

## Reading the caller

The verified caller is put in `request.user` and `request.auth`, which is where
Starlette, FastAPI and Litestar look.

| | FastAPI | Litestar | Starlette |
|---|---|---|---|
| read the caller | `principal: CurrentPrincipal` | `request.user` | `request.user` |
| the caller, if any | `principal: OptionalPrincipal` | `request.user` | `request.user` |
| JWT claims, typed | `claims: Claims` | `request.user` | `request.user` |
| require scopes | `dependencies=[Authenticated(scopes=[...])]` | `guards=[Authenticated(scopes=[...])]` | `@Authenticated(scopes=[...])` |
| public route | `dependencies=[Anonymous()]` | `opt=Anonymous()` | `exclude=` |

On Litestar, `Anonymous()` is `{"exclude_from_auth": True}`, the key Litestar's
own authentication middleware reads, so a handler written for that one is
public here too.

`CurrentPrincipal` is a `Principal`: `subject`, `issuer`, `scopes` and
`claims`, whatever proved who the caller is. Key a caller by `issuer` and
`subject` together, because a subject is unique only within the issuer that
assigned it.

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
| No credential, or another scheme such as `Basic` | `401` | [`authentication-required`](errors.md#authentication-required) | `Bearer`, with `scope=` when the route declares scopes |
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
user signed out everywhere.

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
can choose would refuse somebody else. Read
[Shedding a caller that keeps forging](../security/jwt.md#shedding-a-caller-that-keeps-forging)
for the thresholds.

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
