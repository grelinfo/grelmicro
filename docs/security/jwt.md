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

## Reject a token

Anything that fails raises `TokenRejectedError`. Its `reason` is a stable tag,
so you branch on it instead of matching message text.

```python
from grelmicro.security import TokenRejectedError

try:
    claims = verifier.verify_header(request.headers.get("authorization"))
except TokenRejectedError as error:
    if error.reason == "expired":
        ...
```

The reasons are `algorithm`, `audience`, `expired`, `invalid`, `malformed`,
`missing-claim`, `not-yet-valid`, `scheme`, `signature`, `subject`, `issuer`
and `unknown-key`.

Neither the tag nor the message quotes the token. It is a live credential and
the message reaches your logs.

## What gets checked

Every verification checks the signature against the key the `kid` names, then
`exp`, `nbf`, and any `aud` and `iss` you configured.

`exp` is always required. A token with no expiry is rejected rather than
trusted forever, and there is no setting that turns that off. Set `leeway` to
allow clock skew between the issuer and your service.

The algorithm is pinned to the key. A token asking for a different one is
rejected before its signature is checked, so `none` and the HMAC confusion
attacks have nothing to reach.

### Audience and issuer

Naming an `audience` or an `issuer` requires that claim. A token that simply
omits `aud` does not slip past a configured audience, because a check that
applied only to the tokens carrying the claim would be the wrong way round.

```python
JWTConfig(keys=[...], audience=["my-api"], issuer=["https://auth.example.com/"])
```

That rejects a token with no `aud`, a token with no `iss`, and a token naming
either differently.

Leave `audience` empty for a provider whose tokens carry none. An AWS Cognito
access token names the application in `client_id` instead, so declaring an
audience would reject every one of them.
[RFC 7519](https://datatracker.ietf.org/doc/html/rfc7519#section-4.1.3) also
requires refusing a token whose `aud` the service does not answer to, so an
empty `audience` rejects any token that does carry one.

### Required claims

`required` covers any claim, not only the ones the RFC registers. It only ever
adds: `exp` is enforced whether or not you name it, and so is any `audience`
or `issuer` you configured.

```python
JWTConfig(keys=[...], audience=["my-api"], required=["tenant"])
```

That rejects a token with no `tenant`, a token whose `tenant` is `null`, and
a token with no `exp`.

## Keys

List one `JWTKey` per key you accept. A key with a `kid` serves tokens whose
header names it, and a key without one serves tokens that carry no `kid`.

```python
JWTConfig(
    keys=[
        JWTKey(algorithm="RS256", key=current_pem, kid="2026-09"),
        JWTKey(algorithm="RS256", key=previous_pem, kid="2026-06"),
    ],
    audience=["grelmicro-api"],
)
```

Both keys stay live, so a rotation does not reject the tokens issued before it.

### From a JWKS document

An OIDC provider publishes its keys as a JWKS document. Hand one you already
have to `from_jwks`, which reads the `kid` and `alg` off each key and skips the
ones published for encryption.

```python
verifier = JWTVerifier(
    JWTConfig.from_jwks(jwks, audience=["my-api"], issuer=[issuer])
)
```

### From a JWKS URL

`JWKSVerifier` fetches the document itself and follows the provider when it
rotates. `refresh` is a coroutine, `verify` is not, so no request ever waits on
the provider.

```python
--8<-- "security/jwks.py"
```

`refresh` fetches only when the keys are stale, so a task calling it every
minute costs nothing and bounds how long a rotation takes to reach you. Call
it once before serving too, so the first request does not arrive before the
keys do.

Nothing fetches on the request path. A token naming a key the verifier does
not hold is refused and marks the key set stale, so the next scheduled refresh
picks the new keys up. `retry_interval` puts a floor under how often that can
happen, so a caller inventing `kid` values cannot make your service hammer
your provider.

A refresh that fails raises `JWKSUnavailableError` and leaves the loaded keys
in place, so a provider outage does not take authentication down with it.

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

verifier = JWKSVerifier(config, fetch=fetch)
```

A fetcher that goes through your own client keeps the request inside whatever
OpenTelemetry instrumentation that client already has, so a slow or failing
provider shows up in your traces.

## AWS Cognito

Cognito serves its keys at
`https://cognito-idp.{region}.amazonaws.com/{pool}/.well-known/jwks.json` and
publishes `alg` on every key.

An access token carries no `aud` claim. It names the application in `client_id`
instead, so check that yourself and name `aud` in `required` only for ID
tokens.

```python
issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"
verifier = JWKSVerifier(
    JWKSConfig(
        url=f"{issuer}/.well-known/jwks.json",
        issuer=[issuer],
        required=["token_use"],
    )
)
await verifier.refresh()

claims = verifier.verify(token)
if claims.raw["token_use"] != "access" or claims.raw["client_id"] != client_id:
    raise TokenRejectedError("audience")
```

## Microsoft Entra ID

Entra serves its keys at
`https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys` and publishes
signing keys with no `alg`. `from_jwks` reads the algorithm the key type
implies, and `algorithm=` pins one explicitly.

```python
tenant_issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
verifier = JWKSVerifier(
    JWKSConfig(
        url=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        algorithm="RS256",
        audience=[client_id],
        issuer=[tenant_issuer],
    )
)
await verifier.refresh()
```

## Repeated tokens

A client resends one token until it expires, so most verifications are repeats.
The cache answers a repeat in about 320 nanoseconds against 12 microseconds for
a full verification.

An entry expires at whichever comes first, the token's own `exp` or
`cache_ttl`. The TTL is what bounds a long-lived token: without it a token with
a 24 hour lifetime would keep being accepted from memory for 24 hours after it
was withdrawn upstream.

```python
JWTConfig(keys=[...], audience=["my-api"], cache_size=1024, cache_ttl=300)
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
JWTConfig(keys=[...], audience=["my-api"], cache_key="token")
```

## Shedding a caller that keeps forging

Verifying a forged token costs about what verifying a real one costs, because
the signature has to be checked before any claim can be trusted. A caller
sending forged tokens therefore buys real work per request. `ClientBans`
counts those failures and refuses the caller for a while, which turns that
cost into a dictionary lookup.

It is off unless you ask for it. Pass a `ClientBans` to the verifier, and
give every call the address to hold responsible:

```python
from grelmicro.security import ClientBans, ClientBannedError

verifier = JWTVerifier(config, bans=ClientBans())

try:
    claims = verifier.verify_header(authorization, client=client_ip)
except ClientBannedError:
    raise HTTPException(status_code=429) from None
except TokenRejectedError as error:
    raise HTTPException(status_code=401, detail=error.reason) from None
```

Counting the failure and refusing the client happen for you, so the
protection cannot be half wired. A verifier built with `bans` and then called
without a `client` raises rather than quietly counting nothing.

`ClientBannedError` is not a `TokenRejectedError`. It says nothing about the
token, so answer it with `429` and not `401`: a fresh token would not change
the answer.

`JWKSVerifier` takes the same argument and behaves the same way.

The address has to be one the caller cannot choose. Pass what
[`resolve_client_address`](clientip.md) returns, never a raw
`X-Forwarded-For`, or an attacker sets a header and gets somebody else
refused.

The table is also usable on its own, through `banned()` and `record()`, for
an authentication scheme this module does not handle.

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

Only `signature`, `malformed` and `algorithm` are counted by default. Each
means the token was never issued by anyone the service trusts.

The reasons left out matter more. `unknown-key` is what every client sees for
a moment when the provider rotates its signing keys. Counting it bans a
service's own users on every rotation: with five hundred clients retrying
while a rotation lands, counting rejections by reason bans none of them, and
counting every `401` bans all five hundred.

`expired` is a client that needs to refresh. `not-yet-valid` is a clock that
disagrees. `audience` and `issuer` are a token meant for a neighbouring
service. None of them is an attack.

Pass `reasons=` to choose a different set, and keep `duration` short. An
address is shared behind NAT, so a ban reaches more people than the one caller
that earned it.

## Routing on an untrusted header

`unverified_header` reads `alg` and `kid` without checking the signature. Use
it to route a token to the right verifier, never to decide whether a token is
valid.

```python
kid = verifier.unverified_header(token)["kid"]
```
