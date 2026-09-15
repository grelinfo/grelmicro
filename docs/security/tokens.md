# Outbound Tokens

A service that calls another API needs a token for it. grelmicro gets that
token from your authorization server, caches it, and refreshes it before it
expires.

grelmicro still issues nothing and runs no login. The authorization server
issues the token. Your service asks for it as a client.

There are two ways to get one, and a service uses either or both:

| Pattern | The token says | Use it when |
| --- | --- | --- |
| `ClientCredentials` | "orders-api is calling" | The other API authorizes your service. This is most calls. |
| `TokenExchange` | "orders-api is calling for this user" | The other API needs to know the user too. |

## Install

Tokens are fetched with `httpx` or `httpx2`, whichever your application
already has. A client secret or an assertion file needs nothing else.

A private key signs in the compiled core, which ships as its own wheel:

```bash
pip install "grelmicro[jwt]"
```

## Register the client

Your service is registered at the authorization server as a client. Register
it once in the app, next to everything else:

```python
from grelmicro import Grelmicro
from grelmicro.security import ClientAuth, OAuthClient

micro = Grelmicro(
    uses=[
        OAuthClient.discover(
            "https://auth.example.com/",
            client_id="orders-api",
            client_auth=ClientAuth.secret(),
        ),
    ]
)
```

`discover` finds the token endpoint in the issuer's metadata, the same way
[`JWTVerifier.discover`](jwt.md#from-an-issuer) finds the keys. It reads the
metadata when the app opens. `ClientAuth.secret()` reads the secret from
`GREL_OAUTHCLIENT_CLIENT_SECRET`.

## Call an API as your service

Name the API you call, and hand the pattern's `auth()` to the client that calls
it:

```python
--8<-- "security/tokens.py"
```

Every request `payments` sends carries a token issued for `payments-api`. The
first request fetches it. The ones after it reuse it.

`ClientCredentials` finds the registered `OAuthClient` through the app, the way
`Lock` finds its backend. Pass `client="partner"` to use a client registered
with `name="partner"`, or the `OAuthClient` itself.

`auth()` is how a token should reach a request. It sets the right scheme and
handles a refused token. To read the token for another client, await `token()`:

```python
token = await payments_token.token()
headers = {"Authorization": f"{token.token_type} {token.value}"}
```

`token.expires_at` is when it expires and `token.scopes` is what the server
granted. Its `repr` never shows the value.

## Call an API for the user

`TokenExchange` trades the token your service received for one issued to the
next API, with the user still its subject:

```python
--8<-- "security/tokens_exchange.py"
```

`CurrentToken` is the bearer token
[`AuthenticatedRequests`](../http/authentication.md) verified for this request,
next to `CurrentPrincipal`. On Starlette and Litestar, read it with
`current_token(request)`.

Only a token `AuthenticatedRequests` verified can be exchanged. Your service
never trades a credential it did not check.

An exchanged token is cached per user and API, and never outlives the token it
was exchanged for. The cache keys on a digest of that token, never on the token.
A caller whose token carries no expiry, such as an opaque one, is not cached.

`TokenExchange` follows [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693),
which Keycloak and Okta support. Microsoft Entra ID uses the on-behalf-of grant
instead:

```python
TokenExchange.on_behalf_of("payments-api", scopes=["api://payments/.default"])
```

## Naming the API you call

Providers name the target differently. Pass what yours expects:

| Argument | Sent as | Providers |
| --- | --- | --- |
| `audience=` | `audience` | Auth0, Keycloak, Okta |
| `resource=` | `resource`, [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) | Okta, PingFederate |
| `scopes=` | `scope` | Microsoft Entra ID, AWS Cognito |

## Authenticating your service

Here the client is your service, as the authorization server knows it. Pick how
it proves who it is:

```python
ClientAuth.secret()
ClientAuth.private_key(private_pem, algorithm="RS256", kid="2026-09")
ClientAuth.assertion_file()
```

- `secret` sends the client secret. It reads
  `GREL_OAUTHCLIENT_CLIENT_SECRET` unless you pass the secret. It is sent the
  way the server's metadata says it accepts, in `Authorization: Basic` when the
  server lists both. `method="basic"` or `method="post"` pins it for a server
  that publishes no metadata.
- `private_key` signs a one-minute assertion with your key, as
  [RFC 7523](https://www.rfc-editor.org/rfc/rfc7523) defines, with a fresh
  `jti` every time. The key only comes from code.
- `assertion_file` sends a signed assertion read from a file on every fetch.
  It reads the path from `GREL_OAUTHCLIENT_ASSERTION_FILE` unless you pass it.

Code chooses how your service authenticates. The environment only supplies the
secret or the path. No log line, error message or `repr` shows a secret, a key,
an assertion or a token.

### Which audience a signed assertion names

The assertion names the authorization server it is meant for. The update to
RFC 7523, [RFC 7523bis](https://datatracker.ietf.org/doc/draft-ietf-oauth-rfc7523bis/),
approved and awaiting publication, requires the issuer, because an assertion
naming an endpoint URL can be replayed by a server that claims that URL. That is
the default.

Okta and Microsoft Entra ID still require the token endpoint. Say so:

```python
ClientAuth.private_key(private_pem, algorithm="RS256", audience="token_endpoint")
```

A server that refuses an assertion naming the issuer raises
`ClientRejectedError` with a message suggesting `audience="token_endpoint"`.

Not every provider accepts every algorithm. Okta takes RSA and ECDSA only.

### Kubernetes workload identity

On Kubernetes, point `assertion_file` at a projected service account token whose
audience your authorization server expects. The kubelet rotates the file, and
every fetch reads it again.

Microsoft Entra ID accepts it through federated credentials, from Azure
Kubernetes Service or any other cluster. On Azure Kubernetes Service, the
workload identity webhook names the file:

```bash
GREL_OAUTHCLIENT_ASSERTION_FILE=$AZURE_FEDERATED_TOKEN_FILE
```

Keycloak 26.6 and later accept it through federated client authentication.

EKS and GKE workload identity are not OAuth client authentication. They trade
the token for cloud credentials, which their own SDKs do.

### Rotating a secret

A client secret mounted from a Kubernetes Secret rotates without a restart.
Read the mounted directory with
[`ExternalConfig`](../configuration/reconfigure-from-configmap.md), and a new
`GREL_OAUTHCLIENT_CLIENT_SECRET` file applies to the next fetch. A token already
cached stays valid until it expires.

## Microsoft Entra ID

Name the tenant by its ID. The shared `common` and `organizations` endpoints
publish an issuer holding a `{tenantid}` placeholder, which never matches, so
discovery refuses them.

```python
OAuthClient.discover(
    f"https://login.microsoftonline.com/{tenant_id}/v2.0",
    client_id=client_id,
    client_auth=ClientAuth.private_key(
        private_pem,
        algorithm="PS256",
        certificate=certificate_pem,
        audience="token_endpoint",
    ),
)
```

Entra finds the key by its certificate rather than by `kid`, which is what
`certificate=` provides.

## Refresh

A token is refreshed shortly before it expires, by the first request that finds
it close to expiring. That request, and every request after it, is served the
cached token while one fetch runs. A burst of requests costs the authorization
server one fetch, and a request cancelled while it waits does not cancel the
fetch the others are waiting on.

A server that says when to refresh, with `refresh_in`, is followed. Otherwise
the token refreshes `refresh_before` seconds ahead, at most half its lifetime,
at a slightly random point so replicas started together do not all ask in the
same second.

## When the authorization server fails

A refresh that fails keeps serving the cached token until it expires.

A fetch that loses its connection before any answer is sent again at once, one
time. A token request is safe to repeat, since the worst case is a second token.
A timeout is not retried.

Once no valid token is left, `token()` and `auth()` raise
`TokenUnavailableError`. Failures are remembered so requests fail at once
instead of each waiting on a server that is down:

| Failure | Remembered for | Applies to |
| --- | --- | --- |
| No answer, a timeout, a `408` or a `5xx` | `retry_interval` | every token of this client |
| `429` or `503` with `Retry-After` | what the server asked, at most an hour | every token of this client |
| Any other refusal, such as `invalid_grant` or a `403` | `retry_interval` | that token only |
| `invalid_client`, the service itself refused | `retry_interval` | every token of this client |
| `unauthorized_client`, a grant the service may not use | `retry_interval` | every token of that grant |

A server that is down fails every token at once, while one user whose exchange
is refused does not block anyone else.

`ClientRejectedError`, a `TokenUnavailableError`, means the server refused your
service itself: a wrong secret, a key it does not know, or a client not allowed
this grant. Waiting does not fix it, so it is its own error. `error.error` holds
the code the server sent, such as `invalid_client`. The server's description is
kept on the error and never logged, because a server can echo input into it.

## When the API refuses the token

An API can refuse a token before its `exp`, when its keys rotate or the token is
revoked. On a `401` with `error="invalid_token"`, `auth()` drops the cached
token, so the next request gets a new one.

It sends the refused request again, once, only when the method is safe to
repeat: `GET`, `HEAD`, `OPTIONS`, `PUT` or `DELETE`. A `401` does not prove the
request was not processed, since a gateway can authenticate after acting, so a
`POST` is never sent twice. A request whose body cannot be sent twice, such as
a streamed upload, is not retried either. To retry a `POST` you know is safe,
catch the `401` and send it again yourself.

Many requests refused at once cost one fetch, not one each.

## Proxies and certificates

Tokens are fetched over `https`, and redirects are not followed: a token
endpoint that answers from somewhere else is somewhere else. The client reads
the standard proxy and certificate variables, `HTTPS_PROXY` and
`SSL_CERT_FILE`, from the environment.

## Seeing what it does

Each fetch counts `grelmicro.oauth_client.fetches`, with the client, the grant
and the outcome, and records `grelmicro.oauth_client.fetch.duration`. A request
that reuses a cached token records nothing.

Each fetch is one span, and no span carries a token, a secret or an assertion.
`ClientRejectedError` also writes a record on the `grelmicro.security.events`
logger, because a client the server no longer accepts is often a rotated or
leaked credential.

## Settings

| Setting | On | Default | What it bounds |
| --- | --- | ---: | --- |
| `refresh_before` | `OAuthClient` | `60` | Seconds before expiry a token is refreshed, at most half its lifetime |
| `default_lifetime` | `OAuthClient` | `300` | Lifetime of a token sent without `expires_in` |
| `timeout` | `OAuthClient` | `5` | Wait on the authorization server |
| `retry_interval` | `OAuthClient` | `5` | How long a failure is remembered |
| `cache_size` | `TokenExchange` | `1024` | Exchanged tokens held in memory |

## Configure from the deployment

Leave a setting out of the code for the deployment to supply it. A keyword
always wins:

```python
OAuthClient.discover(client_auth=ClientAuth.secret())
ClientCredentials("payments-api")
```

```bash
GREL_ENV_LOAD=1
GREL_OAUTHCLIENT_ISSUER=https://auth.example.com/
GREL_OAUTHCLIENT_CLIENT_ID=orders-api
GREL_OAUTHCLIENT_CLIENT_SECRET=...
GREL_CLIENTCREDENTIALS_PAYMENTS_API_AUDIENCE=payments-api
```

`GREL_OAUTHCLIENT_ISSUER` is refused for a client built with `endpoint`, and a
secret set for a client that signs its assertions is refused rather than
ignored.

## Without discovery

Give `OAuthClient.endpoint` the token endpoint when the server publishes no
metadata:

```python
OAuthClient.endpoint(
    "https://auth.example.com/oauth2/token",
    client_id="orders-api",
    client_auth=ClientAuth.secret(method="post"),
)
```

A signed assertion needs the issuer to name as its audience, so pass `issuer=`
too, or `audience="token_endpoint"`.

## Without an app

Open the client yourself and pass it to the pattern:

```python
oauth = OAuthClient.discover(...)
payments_token = ClientCredentials("payments-api", client=oauth, audience="payments-api")

async with oauth:
    token = await payments_token.token()
```

## When your platform already does this

A service mesh or an API gateway can attach a client credentials token to
outgoing calls for you. If one token per upstream API is all you need and your
platform already injects it, use that and skip this page.

Use grelmicro when a call acts for a user, when your service signs its
assertions or uses workload identity, when a handler needs to see why a token
was refused, or when there is no mesh.

## Not covered

- Sender-constrained tokens, DPoP proofs and mutual TLS client authentication.
  They follow inbound support for the same tokens.
- Acting for a user after their token expired, in long-running work. An
  exchanged token never outlives the caller's.
- Sharing tokens across replicas. Each replica fetches its own, so no store holds
  live tokens. A provider that bills or caps token issuance counts one fetch per
  replica per API per token lifetime.
- Transaction tokens, which carry the caller through a trust domain in their own
  header. They would be a pattern of their own.
