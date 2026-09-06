# Where a rule applies

Five HTTP components act on requests: the response cache, conditional
requests, idempotent requests, rate limiting, and the access log. Each one
answers the same two questions. This page answers them once, so you learn it
here and every page after this only lists its own fields.

## Two words, on all of them

```python
micro = Grelmicro(
    uses=[
        ConditionalRequests(include=("/carts/*",), exclude=("/carts/legacy",)),
        IdempotentRequests(include=("/payments/*",)),
        RateLimitedRequests(burst, exclude=("/livez", "/readyz")),
        AccessLog(exclude=("/livez", "/readyz")),
    ]
)
```

`include` names the paths a component acts on. Empty means every path.

`exclude` names the paths it leaves alone, and it wins over `include`, so you
can name a whole router and carve one route out of it without the two rules
fighting.

A pattern is an exact path, unless it ends with `*`, which matches as a
prefix. `"/payments/*"` covers everything under `/payments`, and `/payments`
itself, which is how a router mounted there is written.

The path is the one your routes are declared under, not the one on the wire.
A prefix a mount or a proxy adds is taken off first, so the same pattern works
whether the app is served at the root, mounted under another app, or sitting
behind an ingress.

!!! warning "A string is not a set of paths"
    `exclude="/internal/*"` is a missing comma. A string is a sequence of
    characters, so it would be read one character at a time, and the `*`
    would match every path. It is refused where it is written, rather than
    quietly turning the component off. Write `exclude=("/internal/*",)`.

## The response cache says more per path

The cache is the one of the five with something to say about each path, so
its `include` also takes a mapping from pattern to seconds:

```python
CachedResponses(include=("/products/*",))                 # each kept for ttl
CachedResponses(include={"/catalog": 300, "/products/*": 60})
```

The most specific pattern decides, so `{"/products/*": 60, "/products/hot":
300}` keeps the hot one for 300 seconds.

## What wins

Four things can name one endpoint. In order, first one that answers:

1. **`exclude`**, always. Nothing overrides it.
2. **The route's own declaration**, `dependencies=[CachedResponse(ttl=60)]`.
3. **The nearest declaration above it**: a route beats the router it sits in,
   and an inner router beats the one that includes it.
4. **The most specific pattern** in `include`.

A route that declared something is never overridden by a pattern naming it.
The declaration is nearer to the endpoint, and it was written by the person
who knows what the endpoint answers.

## Reading it back

Nothing above is written twice, which means no single place restates it. So
ask the app instead:

```bash
python -m grelmicro check app:micro --app app:app
```

```
Endpoints
  GET    /products      access-log  cache 60s   rate-limit burst, daily
  GET    /products/hot  access-log  cache 300s  rate-limit burst, daily
  POST   /orders        access-log  idempotent 3600s  rate-limit burst, daily
  GET    /livez         -
```

One line per endpoint, read from the routes the app declares and the
components registered beside them. It is computed, so it cannot drift.

A pattern gets no row of its own, because rows come from routes. One that
matches no route at all is reported beside the table instead, as a check: a
mistyped pattern turns a rule off without saying so, and nothing matching is
the only trace it leaves.

`micro.describe(app).endpoints` returns the same thing as data.

## Changing it without a restart

Every field on this page is a value, so a mounted ConfigMap retunes it while
the service runs:

```yaml
grel:
  cached_responses:
    ttl: 60
    include:
      "/products/*": 60
      "/products/hot": 300
  rate_limited_requests:
    exclude: ["/livez", "/readyz"]
  access_log:
    exclude: ["/livez", "/readyz"]
```

A flat `GREL_...` key says the same thing in JSON, which is what a variable
can carry: `GREL_ACCESS_LOG_EXCLUDE='["/livez", "/readyz"]'`.

Register [`ExternalConfig`](../configuration/reconfigure-from-configmap.md)
and the next request is answered with the new values. Nothing is rebuilt: the
middleware reads a snapshot the component publishes, so a request already
running finishes on the configuration it started with.

### What a file may not change

**Live reload tunes what a request costs, never what it is protected by.**

| Component | What it protects | Where it is changed |
|---|---|---|
| `CachedResponses` | latency | live |
| `RateLimitedRequests` | the service's capacity | live |
| `AccessLog` | nothing, it observes | live |
| `ConditionalRequests` | the client's data, from a lost update | a deploy |
| `IdempotentRequests` | the client's data, from a duplicate | a deploy |

A cache miss runs the handler, so turning caching off costs time and nothing
else. Take a path out of idempotency and the next retry runs the operation a
second time, which for a payment is the outcome idempotency exists to
prevent. Take one out of conditional requests and an unconditional write
erases an update nobody is told about.

So those two are configured at startup, every field of them, and changed by
a deploy where they are reviewed. Not a chosen list of fields: every one of
theirs turns out to protect something. Turn off `fingerprint_body` and a key
reused with a different payload replays the first response. Lower
`max_body_size` and a large response stops being stored, or stops carrying
an `ETag`. A list would also have to be maintained, and the field somebody
forgets to add to it is the one a file can then reach.

This is an authorization boundary as much as a design one. Editing a
ConfigMap is a much more widely granted permission than shipping an image,
so what a file may change is what somebody with only that permission may
change.

The same holds for what a client has to send. The OpenAPI schema is built
once, from the app as installed, because it is a published contract rather
than a tuning knob, and each replica polls its own source on its own clock,
so a live schema would have two pods publishing two different documents.
Nothing the schema states is live either: `require_key`, the header names,
and `reused_status` move only with a deploy.

`RateLimitedRequests` keeps its reach live, because its contract has a
superset form: the schema documents the `429` on every operation, which stays
true whichever paths are metered. A rule that says what a client *must send*
has no superset form, which is why the other two are fixed.

A key a file may not change is reported, naming the variable, and every
other key in the same file still applies.

And a file never changes what the app is made of. It cannot register a
component, choose a cache store, add a rate limiter, or supply a `key=`
function. So a cache pattern arriving from a ConfigMap is checked against
the same rules `micro.install(app)` checks: one naming a write, or a read
behind a security scheme, is refused and the running configuration is kept.

## Where the budget lives

Two settings look like they belong here and do not.

A rate limit is held by the `RateLimiter`, not by the middleware, because the
buckets are the limiter's. It is tuned under its own name:

```yaml
grel:
  ratelimiter:
    burst:
      limit: 500
```

An idempotency window is held by the `Idempotency` that stores the response,
for the same reason. `IdempotentRequests` names its one after the namespace
it stores under, so the key is `grel.idempotency.http.ttl`.

The split is the point. The HTTP component says *where* a rule applies and
what one request costs. The object underneath says what the budget is.
