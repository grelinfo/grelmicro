# Outbound tokens

A service that calls another API authenticates the call. The
[Outbound Tokens](../security/tokens.md) page shows how. This page records why
the API has the shape it has, which choices were weighed and dropped, and what
waits for a later version.

grelmicro still issues no token and runs no login. It asks an authorization
server for a token as a client, the way it asks a JWKS endpoint for keys.

## Two grants, two patterns

A service calls another API in one of two ways:

- As itself. The API authorizes the service, and no user is involved. This is
  most calls, and the only option for work with no request behind it, such as a
  task, an outbox relay or a leader-elected job.
- For a user. The API needs to know the user, so the token the service received
  is exchanged for one issued to the API.

The two are independent by protocol. Microsoft Entra ID's on-behalf-of grant
works only for users, and a caller with no user must use client credentials.
So each grant is its own pattern, `ClientCredentials` and `TokenExchange`, and a
service uses either one or both. Neither sets the other up.

One source with an `on_behalf_of=` argument was the first draft and was dropped.
It made exchange a mode of a client credentials source, so a service that only
exchanges still configured a grant it never used.

## The client is a component, the target is a pattern

What a service tells the authorization server splits in two:

| Held once, on `OAuthClient` | Held per API, on the pattern |
| --- | --- |
| issuer or token endpoint | `audience`, `resource` or `scopes` |
| `client_id` and how the client authenticates | the pattern's name |
| timeouts, refresh settings, failure memory | the exchange cache |

`OAuthClient` is a component, registered in `uses=` and opened by the app. The
patterns find it through the app the way `Lock("cart")` finds its backend, and
take `client=` for a second client or one built outside an app. A service
calling three APIs registers its credentials once and names three patterns.

The pattern is named after the API it calls, which follows
[a pattern's name is what it protects](api-conventions.md#a-patterns-name-is-what-it-protects).
That name is also its environment prefix, so a deployment can set
`GREL_CLIENTCREDENTIALS_PAYMENTS_API_AUDIENCE`. A target passed on every call
could not be set from the deployment at all.

A provider was the other candidate for `OAuthClient`. Providers own a vendor
connection and read the vendor's own variables, such as `REDIS_URL`.
`OAuthClient` reads `GREL_OAUTHCLIENT_`, like every component.

## The grant is a factory, not a vendor

`TokenExchange` follows [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693) by
default. The on-behalf-of grant, which Microsoft Entra ID uses, is
`TokenExchange.on_behalf_of`. The factory names the grant, the way
[algorithms use factory classmethods](api-conventions.md#algorithms-use-factory-classmethods).

A factory named after a vendor was dropped. A provider's quirk on the inbound
side is a setting, `algorithm=` on `JWTVerifier.discover`, not a
`JWTVerifier.entra`. The same holds here.

`TokenExchange` takes no generic `requested_token_type=`. A transaction token,
which carries the caller through a trust domain in its own header, is not an
exchange for a target API, and a type switch here would make it look like one.
If it comes, it is a pattern of its own.

## A token reaches a request through `auth()`

`token()` returns an `AccessToken` carrying `token_type` next to `value`, and
the documented way to attach it is `auth()`. A sender-constrained token uses
another scheme and a proof per request, so code that writes `Bearer` by hand
would break without a word the day one arrives. `auth()` is where that change
would land.

`httpx` checks an auth object with `isinstance` against its own `Auth` class,
and `httpx2` against its own. An `httpx.Auth` subclass handed to an `httpx2`
client is refused. Starlette's `full` extra installs both lines, so either can
be the client an application uses. `auth()` builds one class inheriting from
every `Auth` installed, and both clients accept it.

## Code chooses how the service authenticates

`client_auth=` is always code: `ClientAuth.secret()`, `.private_key(...)` or
`.assertion_file()`. The environment supplies the secret and the file path, and
never the method. It is the [configuration](config.md) rule that the
environment tunes a value and never chooses an algorithm.

A private key comes from code only, as `JWTKey` material does. A client secret
is read from the environment, because a mounted Kubernetes Secret is how one
arrives, and it is held as a `SecretStr` that no error message repeats. It is a
live setting, so `ExternalConfig` reading a mounted Secret directory rotates it
without a restart. A second way to read a secret from a file was not added,
because that one already exists.

## Basic or body

[RFC 6749](https://www.rfc-editor.org/rfc/rfc6749#section-2.3.1) requires
servers to accept a secret in `Authorization: Basic`. The OAuth 2.1 draft
reverses that: servers must accept it in the request body, and Basic is
optional. A fixed default is wrong for one of the two.

So `ClientAuth.secret()` sends the secret the way the server's metadata lists in
`token_endpoint_auth_methods_supported`. When both are listed it uses Basic,
which every server predating OAuth 2.1 must accept. When the list is absent it
uses Basic, as [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) says to.
`method=` pins it. Basic form-encodes the client ID and secret before encoding
them, which a hand-written client often misses.

## Which audience a signed assertion names

A signed client assertion names the server it is meant for in `aud`. The
original RFC 7523 allowed the token endpoint URL. In 2025 researchers showed
that a malicious authorization server can publish an honest server's token
endpoint as its own and replay the assertion it receives there.
[RFC 7523bis](https://datatracker.ietf.org/doc/draft-ietf-oauth-rfc7523bis/),
approved and awaiting publication, closes it: the assertion names the issuer,
which discovery has validated, as its only audience, and declares
`typ: client-authentication+jwt`.

That is the default. Okta and Microsoft Entra ID still document the token
endpoint and refuse the issuer, so `audience="token_endpoint"` names it, and a
refusal of an issuer-audience assertion says to try it. An assertion naming the
endpoint carries no `typ`, because the type promises the new rules, and the
update asks servers not to refuse an assertion for lacking one.

Every assertion lives one minute and carries a fresh `jti`. Keycloak refuses a
reused `jti`, and Auth0 caps the lifetime at five minutes. An assertion is never
cached.

`certificate=` adds the `x5t#S256` header, the SHA-256 thumbprint of the
certificate, because Microsoft Entra ID finds the key by its certificate rather
than by `kid`. A SHA-1 `x5t` is not sent. Entra documents the SHA-256 form, and
the SHA-1 one survives only for AD FS, which this feature does not target.

A private key is read from PEM only. Reading a private JWK would mean converting
it to DER by hand, which is the most sensitive code this feature could contain,
and PEM is what providers hand out.

## The assertion file is read on every fetch

The kubelet rotates a projected service account token in place. Reading the
file once would send an expired assertion within the hour, so every fetch reads
it again.

Only authorization servers that accept a Kubernetes token as a client assertion
work this way: Microsoft Entra ID through federated credentials, and Keycloak
26.6 and later. EKS and GKE workload identity trade the token for cloud
credentials through their own token services, which is not OAuth client
authentication, so the page does not claim them.

## The caller's token is passed, never ambient

`TokenExchange.auth(token)` takes the caller's token from `CurrentToken`. An
`auth()` that read it from the current request was weighed and dropped.

Reading it ambiently means holding it in a context variable. `asyncio` copies
context into every task a request starts, so a task still running after the
response would keep acting as that user, and a shared client used from a
background job would send a stale user's token without a word. Passing the
token makes the user visible at the call that acts for them.

`CurrentToken` holds only a token `AuthenticatedRequests` verified. A token that
did not verify never reaches a handler, so a service never exchanges a
credential it did not check. The authorization server checks the token again,
and grelmicro does not rely on it doing so.

## The exchange cache is bounded by the caller

An exchanged token is cached per caller and API. The entry expires at whichever
comes first, the issued token's expiry or the caller's own `exp`, so a token
issued for a user never outlives the token that user presented. A caller whose
token has no expiry is not cached, because nothing would bound it.

That rules out acting for a user after their token expired, in long-running
work. Supporting it would mean keeping a user's delegated credential past the
proof they gave, which this feature does not do.

The key is a SHA-256 digest of the caller's token, the client ID, the grant and
the target. It never holds the token, for the reason
[JWT verification](jwt.md#the-default-keys-on-a-digest) gives.

## Tokens are not shared across replicas

Each replica caches its own tokens in memory. A shared store would save fetches
on a rolling deploy, and a provider that bills or caps token issuance counts
each one. It would also put live tokens for every downstream API in a store any
reader of that store could use, where today they exist only in the process that
sends them.

The saving is small against that. With one fetch per replica per API per token
lifetime, twenty replicas calling five APIs with hour-long tokens fetch about a
hundred times an hour. Exchanged tokens are never a candidate at all, since a
shared store of them is a store of every active user's delegated access.

## A 401 retries only what is safe to repeat

[RFC 6750](https://www.rfc-editor.org/rfc/rfc6750#section-3.1) lets a client
fetch a new token after `invalid_token` and try again. A `401` does not prove
the request went unprocessed, because a gateway can authenticate after acting,
and [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.2) says a
client should not repeat a non-idempotent request unless it knows the request
was not applied.

So `auth()` drops the cached token on every `invalid_token`, and sends the
request again only for `GET`, `HEAD`, `OPTIONS`, `PUT` and `DELETE`, once, and
only when the body can be sent twice. A `POST /charges` is never sent twice.
Refusals arriving together share one fetch.

Some clients resend any method after a `401`. An opt-in for that was weighed and
not added. Nothing in a request says it is safe to repeat: the `Idempotency-Key`
header draft expired without becoming a standard. A caller who knows a `POST`
is safe can catch the `401` and send it again, and the decision stays at the
call that knows.

## Where signing runs

A signed assertion is made once per token fetch, minutes apart, next to a
network round trip of milliseconds. Speed does not decide where signing runs.
What decides it is that `grelmicro[jwt]` already ships one crypto library in the
compiled core, and signing in Python would add a second one with its own
advisories and its own key parsing to prove. Measured anyway, on the machine
[JWT verification](jwt.md) uses:

| Signing, key already loaded | µs |
| --- | ---: |
| RS256, core | 273 |
| RS256, Python `cryptography` | 959 |
| ES256, core | 10 |
| Ed25519, core | 4 |

The core does not sign through `jsonwebtoken`. Its `encode` parses the key
again on every call, and accepts a corrupt RSA key until the first signature
fails. The signer holds `aws-lc-rs` key objects built once, which check the key
when they are built, so a bad key fails at startup rather than on the first
fetch. Encrypted PEM is refused with an error saying so.

The PEM arrives as a Python string, which nothing can wipe from memory. The docs
claim no zeroization for that reason.

A client secret and an assertion file need no signing, so they need neither the
core nor the extra. The core is imported only by `ClientAuth.private_key`.

## Refresh runs on a request, not a timer

`JWTVerifier` refreshes keys in a background task, because one key set serves
every request. A token source is different: a client can hold many targets and
many exchanged tokens, most of them idle. A timer per token would wake for
tokens nobody uses.

So a token refreshes when a request finds it inside its refresh window. That
request starts one fetch and is served the cached token, and so is every request
while the fetch runs. The fetch is a task the client owns, so cancelling the
request that started it leaves it running for the others, and closing the
client cancels it.

A server that sends `refresh_in` decides the window. Otherwise it is
`refresh_before`, capped at half the token's lifetime. A token living sixty
seconds would otherwise be inside its window the moment it arrived, and every
request would start a fetch. The point inside the window is random, so replicas
started together spread their fetches out.

Discovery runs when the client opens, so the first request after startup waits
for a token and not for metadata too.

An `asyncio` task belongs to one event loop. A call arriving from another loop,
such as a synchronous handler going through a thread, is sent to the loop the
client was opened on rather than awaiting a task it cannot await.

## Failures are remembered at two levels

grelmicro components do not wrap their own I/O in `Retry` or `CircuitBreaker`.
This one follows `JWTVerifier` instead: a `timeout`, one shared fetch per key,
and failures remembered for `retry_interval`.

Remembering matters most when no token is cached. A hundred requests arriving
while the server is down would wait on one five second fetch, and the next
hundred would each start another. Measured: the first hundred cost one fetch and
five seconds, the next hundred cost no fetch and 1.2 milliseconds.

What is remembered, and for whom, depends on what failed:

- A server that does not answer, times out or answers `5xx` is down for every
  token. Remembering it per token would let a thousand users each start a fetch
  against it, so it is remembered for the whole client.
- A `429` or `503` with `Retry-After` is the server asking every caller to wait,
  so it is remembered for the whole client, for as long as it asks and at most
  an hour, which bounds a server sending a mistaken value.
- A refusal of one token, such as `invalid_grant` on one user's exchange, says
  nothing about anyone else, so it is remembered for that token only.

A connection lost before any answer is retried once, at once. It is the one
failure a retry fixes, because a keep-alive connection closed by the other side
is common, and a token request repeated costs at most a second token. A timeout
is not retried, because it already spent the wait.

`invalid_client` and `unauthorized_client` are raised as `ClientRejectedError`.
They are configuration errors, not outages, and retrying them never succeeds.

## Nothing is recorded per request

A request reusing a cached token costs a dictionary lookup, 50 nanoseconds, and
setting a header, about one microsecond through `httpx`. Metrics, spans and
events are recorded per fetch, never per request, so observing the client adds
nothing to that cost.

Trace redaction also hides `client_assertion`, `assertion` and `subject_token`,
the form fields a token request carries.

## A platform can make this unnecessary

A service mesh or a gateway that injects a client credentials token on outgoing
calls covers the simplest case with no code. The ones available today inject a
token fetched with a client secret, and none exchanges a user's token. The user
page says when to use the platform and when grelmicro adds what it cannot.

## What waits, and why

Every item below was proposed during review and left out of the first version.
Each can be added later without changing anything that ships, and none had the
evidence to go in now.

| Waits | Why | What ships now so it can land later |
| --- | --- | --- |
| An actor token on `TokenExchange` | Few servers accept one, and a plain exchange covers the common case. | The exchange cache key is built in one place, so an actor can join it. |
| Signing with a key in a KMS or an HSM | No reference client offers a byte signer, and a private key or workload identity covers most services. | Assertions are built in one function, so a signer can supply only the signature. |
| A SPIFFE assertion type | The client authentication draft has no major server behind it yet. | `assertion_file` sends `jwt-bearer`, and the type is one value in one place. |
| Continuous access evaluation claims challenges | Entra sends them only to clients declaring the `cp1` capability, only from Microsoft Graph, only for single-tenant apps. Without declaring it, a handler never runs. | Nothing. It needs extra token request parameters first. |
| Extra token request parameters, such as Auth0's `organization` | Real demand, but additive, and each provider names them differently. | Token requests are built as a form in one place. |
| `bulkhead=` on `OAuthClient` | No reference client caps token fetches, and fetches are already shared per key. | Nothing. A keyword is additive. |
| `fetch=` for your own HTTP client | The default client already reads the proxy and certificate variables. | Nothing. A keyword is additive. |
| A `stale` outcome and a counter for refused tokens | No reference client records them. | The `outcome` attribute is an open set. |
| Synchronous `httpx.Client` | grelmicro is async first, and a synchronous flow cannot await a shared fetch. | Nothing. |
