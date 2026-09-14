# JWT

[Rust powered](../architecture/jwt.md){ .grel-tag .grel-tag--rust }

A caller presents a bearer token. `JWTVerifier` decides whether to believe it.

grelmicro never issues tokens. It checks the one that arrives: the signature,
the key it was signed with, and the claims that bound when and where the token
is good for.

## Install

Verification runs in a compiled core, which ships as its own wheel.

```bash
pip install "grelmicro[jwt]"
```

## Verify a token

Give the verifier the public key and the claim policy once. It parses every
key at construction, never per request.

```python
--8<-- "security/jwt.py"
```

`verify` is a plain synchronous call. A verification takes about 12
microseconds, so moving it to a thread costs more than it saves. The
[architecture notes](../architecture/jwt.md) have the measurements.

To authenticate every request rather than calling `verify` yourself, register
[`AuthenticatedRequests`](../http/authentication.md).

## Reject a token

Anything that fails raises `TokenRejectedError`. Its `reason` is a
`TokenRejectedReason`, so you branch on it instead of matching message text.

```python
from grelmicro.security import TokenRejectedError, TokenRejectedReason

try:
    claims = verifier.verify_header(request.headers.get("authorization"))
except TokenRejectedError as error:
    if error.reason is TokenRejectedReason.EXPIRED:
        ...
```

Each reason also compares equal to the string it names, so
`error.reason == "expired"` works too. The reasons are `algorithm`,
`audience`, `binding`, `expired`, `invalid`, `issuer`, `malformed`,
`missing-claim`, `not-yet-valid`, `scheme`, `signature`, `type` and
`unknown-key`.

Neither the tag nor the message quotes the token. It is a live credential and
the message reaches your logs.

## What gets checked

Every verification checks the signature against the key the `kid` names, then
`exp`, `nbf` and `iat`, and any `aud` and `iss` you configured. `sub` and `jti`
must be strings and `iat` a number of seconds, as RFC 7519 defines them, and a
token whose `iat` is still to come is not valid yet.

`exp` and `sub` are always required. A token with no expiry is rejected rather
than trusted forever, and one with no subject rather than read as a caller
nobody can name. There is no setting that turns either off. Set `leeway` to
allow clock skew between the issuer and your service.

The algorithm is pinned to the key. A token asking for a different one is
rejected before its signature is checked, so `none` and the HMAC confusion
attacks have nothing to reach.

### Token type

A provider signs more than access tokens with the same keys: DPoP proofs,
logout tokens, security event tokens. Each declares its kind in the `typ`
header, so a token declaring anything other than `JWT`, `JOSE` or `at+jwt` is
rejected with `type`. A token that declares no type passes.

Set `token_type="at+jwt"` to require the access token type of
[RFC 9068](https://datatracker.ietf.org/doc/html/rfc9068#section-4). Microsoft
Entra ID never sends it and Keycloak sends it only when a client asks, so it
is off by default. The audience check is what keeps an ID token out either
way, because an ID token names the client, not your API.

### Bound tokens

A token carrying a `cnf` claim is bound to a key, and is only good together
with proof that the caller holds that key. grelmicro checks no such proof, so
a bound token is rejected with `binding` rather than accepted without the
binding its issuer asked for.

### Audience and issuer

Naming an `audience` or an `issuer` requires that claim. A token that simply
omits `aud` does not slip past a configured audience, because a check that
applied only to the tokens carrying the claim would be the wrong way round.

```python
JWTVerifier.keys(..., audience="my-api", issuer="https://auth.example.com/")
```

That rejects a token with no `aud`, a token with no `iss`, and a token naming
either differently.

Set `audience=None` for a provider whose tokens carry none. An AWS Cognito
access token names the application in `client_id` instead, so declaring an
audience would reject every one of them.
[RFC 7519](https://datatracker.ietf.org/doc/html/rfc7519#section-4.1.3) also
requires refusing a token whose `aud` the service does not answer to, so
`audience=None` rejects any token that does carry one.

### Required claims

`required` covers any claim, not only the ones the RFC registers. It only ever
adds: `exp` is enforced whether or not you name it, and so is any `audience`
or `issuer` you configured.

```python
JWTVerifier.keys(..., audience="my-api", required=["tenant"])
```

That rejects a token with no `tenant`, a token whose `tenant` is `null`, and
a token with no `exp`.

## Reading the caller

`verify` returns a `JWTClaims`. The registered claims are fields, `subject`,
`issuer`, `audience`, `expires_at`, `issued_at` and `token_id`, and `claims`
holds every claim as it arrived, read-only.

`scopes` is what the token grants. It is read from `scope`, then `scp`, as a
space-separated string or an array of strings, so Microsoft Entra ID's `scp`
string and Okta's `scp` array both work. The first of those claims the token
carries decides, and a claim of any other shape grants nothing. Name other
claims with `scope_claims`:

```python
JWTVerifier.keys(..., audience="my-api", scope_claims=["permissions"])
```

`JWTClaims` satisfies `Principal`, the protocol a handler reads the caller
through, whatever proved who it is. Key a caller by `issuer` and `subject`
together, never by an email or a username, which an issuer can reassign.

## Keys

Pass one `JWTKey` per key you accept. A key with a `kid` serves tokens whose
header names it, and a key without one serves tokens that carry no `kid`.

```python
JWTVerifier.keys(
    JWTKey.pem(current_pem, algorithm="RS256", kid="2026-09"),
    JWTKey.pem(previous_pem, algorithm="RS256", kid="2026-06"),
    audience="grelmicro-api",
)
```

Both keys stay live, so a rotation does not reject the tokens issued before it.

### From a JWKS document

An OIDC provider publishes its keys as a JWKS document. Hand one you already
have to `from_jwks`, which reads the `kid` and `alg` off each key and skips the
ones published for encryption.

```python
verifier = JWTVerifier.from_config(
    JWTKeysConfig.from_jwks(jwks, audience="my-api", issuer=issuer)
)
```

### From a JWKS URL

`JWTVerifier.jwks` fetches the document itself and follows the provider when
it rotates. `refresh` is a coroutine, `verify` is not, so no request ever waits on
the provider.

```python
--8<-- "security/jwks.py"
```

Open the verifier with `async with`, in your app's lifespan. It loads the keys
before the first request and refreshes them in the background every
`retry_interval` until it closes. A refresh fetches only when the keys are
stale, so a pass while they are fresh costs nothing, and a rotation reaches you
within one interval.

A provider that cannot be reached at startup does not stop the app. The
verifier opens without keys, every verification raises
`SigningKeysUnavailableError`, and the background refresh keeps trying.

`refresh()` is still yours to await, for a request refused with `unknown-key`
that wants the new keys now. A caller arriving while a fetch runs waits for
that one rather than starting another, so a burst of such requests costs your
provider one fetch.

Nothing fetches on the request path. A token naming a key the verifier does
not hold is refused and marks the key set stale, so the next background
refresh picks the new keys up. `retry_interval` puts a floor under how often that can
happen, so a caller inventing `kid` values cannot make your service hammer
your provider.

A refresh that fails raises `SigningKeysUnavailableError` and leaves the
loaded keys in place, so a provider outage does not take authentication down
with it. Verifying before any key set has loaded raises it too.

The endpoint must be `https`. Bodies are read in chunks and abandoned past
`max_bytes`, redirects are not followed, and a document with more than
`max_keys` keys is refused.

| Setting | Default | What it bounds |
| --- | ---: | --- |
| `ttl` | `3600` | How long a fetched document is current |
| `retry_interval` | `60` | Least time between two fetches |
| `timeout` | `5` | Wait on the endpoint |
| `max_bytes` | `1048576` | Largest document accepted |
| `max_keys` | `32` | Most keys accepted from one document |

The default fetcher uses `httpx`, and takes either `httpx` or `httpx2`,
whichever your application already has. Pass `fetch=` to use your own client
instead, which is how you reuse a proxy, a certificate authority, mutual TLS,
or your existing tracing and retry policy:

```python
async def fetch(url: str, *, timeout: float, max_bytes: int) -> bytes:
    response = await my_client.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content

verifier = JWTVerifier.from_config(config, fetch=fetch)
```

A fetcher that goes through your own client keeps the request inside whatever
OpenTelemetry instrumentation that client already has, so a slow or failing
provider shows up in your traces.

### From an issuer

Most providers say where their keys live. Give `JWTVerifier.discover` the
issuer, and it reads the JWKS URL from the provider's metadata:

```python
verifier = JWTVerifier.discover("https://auth.example.com/", audience="orders-api")

async with verifier:
    claims = verifier.verify(token)
```

It reads the [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) authorization
server metadata first, then the
[OpenID Connect discovery](https://openid.net/specs/openid-connect-discovery-1_0.html)
document, and remembers which one answered.

| Issuer | Looked up at |
| --- | --- |
| `https://auth.example.com/` | `/.well-known/oauth-authorization-server`, then `/.well-known/openid-configuration` |
| `https://auth.example.com/realms/shop` | `/.well-known/oauth-authorization-server/realms/shop`, then `/realms/shop/.well-known/openid-configuration` |

Everything else works as it does for a JWKS URL: `async with`, the background
refresh, `fetch=`, and the limits in the table above.

The document that answers must name the issuer exactly, character for
character, trailing slash included. A document answering for another issuer is
never used, so a misrouted or hostile endpoint never chooses your keys. Like an
error page served with `200`, it is passed over for the next document. The JWKS URL it names must be `https`, and every token must
carry the issuer in `iss`.

The metadata is read again once per `ttl`. A refresh asked for by a token
naming a new key fetches the key set alone. A metadata endpoint that cannot be
reached never holds a rotation up: the key set it last named is fetched, and
the metadata is tried again once `retry_interval` has passed.

## Configure from the deployment

A verifier built by `keys`, `jwks` or `discover` also reads its settings from the
environment once `GREL_ENV_LOAD` is set, under `GREL_JWTVERIFIER_`, or
`GREL_JWTVERIFIER_{NAME}_` for one built with `name=`. A keyword always wins,
so leave a setting out of the code for the deployment to supply it:

```python
verifier = JWTVerifier.discover()
```

```bash
GREL_ENV_LOAD=1
GREL_JWTVERIFIER_ISSUER=https://auth.example.com/
GREL_JWTVERIFIER_AUDIENCE=orders-api
```

A list is written comma-separated or as JSON. A mounted file read by
[`ExternalConfig`](../configuration/reconfigure-from-configmap.md) uses the
same names.

The environment says whose tokens to trust, and nothing more:

| Setting | From the environment | Changed by a mounted file while running |
| --- | --- | --- |
| `url`, `audience`, `issuer`, `required`, `token_type`, `scope_claims`, `leeway`, `cache_key`, `cache_ttl`, `max_bytes`, `max_keys` | at startup | no |
| `cache_size`, `ttl`, `retry_interval`, `timeout` | at startup | yes |
| the key source, `algorithm`, key material, `audience=None` | never | never |

A verifier reads its own prefix only. `GREL_JWTVERIFIER_AUDIENCE` never
reaches a verifier named `partner`, so one verifier's trust settings cannot
leak into another's. A variable naming `ALGORITHM` is refused at startup
rather than applied.

Only code answers to no audience. `audience=None` in code wins over the
environment, and no variable can turn the audience check off.

`ClientBans` reads `GREL_CLIENTBANS_` the same way, and every one of its
settings can change while the service runs, because a ban costs capacity and
never trust.

## AWS Cognito

Cognito publishes OpenID Connect discovery for every user pool, and `alg` on
every key.

An access token carries no `aud` claim. It names the application in `client_id`
instead, so pass `audience=None` and check `client_id` yourself. An ID token
does carry `aud`, so verify one with a verifier built with `audience=client_id`.

```python
verifier = JWTVerifier.discover(
    f"https://cognito-idp.{region}.amazonaws.com/{pool}",
    audience=None,
    required=["token_use"],
)
async with verifier:
    claims = verifier.verify(token)
    if claims.claims["token_use"] != "access" or claims.claims["client_id"] != client_id:
        raise TokenRejectedError("audience")
```

## Microsoft Entra ID

Entra publishes OpenID Connect discovery for each tenant, and signing keys with
no `alg`. `algorithm=` pins the one its keys use.

```python
verifier = JWTVerifier.discover(
    f"https://login.microsoftonline.com/{tenant_id}/v2.0",
    algorithm="RS256",
    audience=client_id,
)
```

Name the tenant by its ID. The issuer in Entra's metadata always carries the
ID, so a tenant named by its domain does not match it and is refused.

## Repeated tokens

A client resends one token until it expires, so most verifications are repeats.
The cache answers a repeat in about 320 nanoseconds against 12 microseconds for
a full verification.

An entry expires at whichever comes first, the token's own `exp` or
`cache_ttl`. The TTL is what bounds a long-lived token: without it a token with
a 24 hour lifetime would keep being accepted from memory for 24 hours after it
was withdrawn upstream.

```python
JWTVerifier.keys(..., audience="my-api", cache_size=1024, cache_ttl=300)
```

Set `cache_size=0` to turn the cache off.

### Sizing it

`cache_size` decides more than any other setting here. Size it above the number
of tokens in flight at once, which is roughly your active callers.

| Traffic | cache_size | hit rate | ns per request |
| --- | ---: | ---: | ---: |
| 20 internal callers | 1024 | 100% | 330 |
| 500 users | 1024 | 99.5% | 395 |
| 5,000 users | 1024 | 61.7% | 4,850 |
| 5,000 users | 8192 | 92.1% | 1,268 |

The same traffic costs 3.8 times less on a cache that fits it. Entries are
small, 65 bytes per key, so a cache of 8192 holds well under a megabyte of
keys.

### Tokens are not held in memory

The cache keys on a SHA-256 digest of the token, so a process holds no live
bearer token beyond the request that presented it. The digest is computed in
the core and costs about 94 nanoseconds on a hit, under 1% of a verification.

Set `cache_key="token"` to key on the encoded token instead, which is faster by
that 94 nanoseconds and is what an in-process cache normally does.

```python
JWTVerifier.keys(..., audience="my-api", cache_key="token")
```

## Shedding a caller that keeps forging

Verifying a forged token costs about what verifying a real one costs, because
the signature has to be checked before any claim can be trusted. A caller
sending forged tokens therefore buys real work per request. `ClientBans`
counts those failures and refuses the caller for a while, which turns that
cost into a dictionary lookup.

It is off unless you ask for it. Check the table before verifying, and record
a rejection after:

```python
from grelmicro.security import ClientBannedError, ClientBans, TokenRejectedError

bans = ClientBans()

if bans.banned(client_ip):
    raise ClientBannedError(retry_after=bans.banned_for(client_ip))
try:
    claims = verifier.verify_header(authorization)
except TokenRejectedError as error:
    bans.record(client_ip, error.reason)
    raise
```

`banned()` is one dictionary lookup, the only cost an honest request pays.
`record()` runs only once a token was already refused, and `banned_for()` only
once a client is refused.

`ClientBannedError` is not a `TokenRejectedError`. It says nothing about the
token, so answer it with `429` and not `401`: a fresh token would not change
the answer. `retry_after` says how long the ban has left.

The address has to be one the caller cannot choose. Pass what
[`resolve_client_address`](clientip.md) returns, never a raw
`X-Forwarded-For`, or an attacker sets a header and gets somebody else
refused.

Settings go in as keywords, `ClientBans(failures=10, window=60, duration=300)`,
or whole, through `ClientBans.from_config(ClientBansConfig(...))`.

### Why not rate limit instead

Rate limiting every request ahead of verification also sheds the load, and
costs more. Measured on one machine, per request:

| Step | ns |
| --- | ---: |
| `banned()` on an honest caller | 55 |
| Verify a token already seen | 299 |
| Rate limiter, in memory | 906 |
| Verify a token for the first time | 11,619 |
| Rate limiter, over Redis | 243,059 |

A limiter in front of verification charges every honest request to shed
traffic that is usually not there, and a distributed one charges twenty times
what the verification it protects costs. Counting failures charges nothing
until a caller has already proven itself, and then charges 88 ns to refuse it.

### What counts as abuse

Only `signature` and `algorithm` are counted by default. Each means a token
was built to pass as one the service trusts, and each costs a full verification
to refuse.

The reasons left out matter more. `unknown-key` is what every client sees for
a moment when the provider rotates its signing keys. Counting it bans a
service's own users on every rotation: with five hundred clients retrying
while a rotation lands, counting rejections by reason bans none of them, and
counting every `401` bans all five hundred.

`malformed` is refused for almost nothing, before any signature is checked,
and a legitimate client sending an opaque token lands there. `expired` is a
client that needs to refresh. `not-yet-valid` is a clock that disagrees.
`audience` and `issuer` are a token meant for a neighbouring service. None of
them is an attack.

Pass `reasons=` to choose a different set, and keep `duration` short. An
address is shared behind NAT, so a ban reaches more people than the one caller
that earned it.

## Routing on an untrusted header

`unverified_header` reads `alg` and `kid` without checking the signature. Use
it to route a token to the right verifier, never to decide whether a token is
valid.

```python
from grelmicro.security import unverified_header

kid = unverified_header(token)["kid"]
```
