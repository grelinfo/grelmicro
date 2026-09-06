# HTTP component internals

This page is the engineering side of [Where a rule applies](../http/where.md).
It documents the contract the five HTTP components follow, and why it is not
the contract the resilience patterns follow.

## The contract

An HTTP component is a **single-instance component** in the sense
[Configuration internals](config.md) defines: `__init__(**kwargs)` and
`from_config(config)`, with no positional name. Its env address is the bare
kind prefix, `GREL_CACHED_RESPONSES_`.

| Component | Kind | Prefix |
|---|---|---|
| `CachedResponses` | `cached_responses` | `GREL_CACHED_RESPONSES_` |
| `ConditionalRequests` | `conditional_requests` | `GREL_CONDITIONAL_REQUESTS_` |
| `IdempotentRequests` | `idempotent_requests` | `GREL_IDEMPOTENT_REQUESTS_` |
| `RateLimitedRequests` | `rate_limited_requests` | `GREL_RATE_LIMITED_REQUESTS_` |
| `AccessLog` | `access_log` | `GREL_ACCESS_LOG_` |

`ErrorResponses` is absent because it holds no value fields. The format comes
from the factory you call, which is structure, so there is nothing to tune.

## Why it is not the multi-instance contract

A resilience pattern takes a positional name because **the name is a thing in
the world**. `Lock("cart")` is a key in the backend. `RateLimiter("burst")` is
a set of buckets, and its name reaches the wire in the `RateLimit` header. Two
names are two runtime objects, so identity is the first parameter.

An HTTP component holds no such thing. It acts on requests passing through, so
its question is **where**, never **which one**. Its vocabulary is the path:
`include`, `exclude`, `methods`, and the framework-native declaration on a
route. Identity never appears in it.

A `name=` keyword survives for a second set of rules on one app, which is rare
and stays a keyword. Borrowing the positional name from the pattern contract
would put an address where a path belongs, and every reader would then look
for the runtime object the name refers to. There is none.

This is also what comparable systems do. A library embedded in someone else's
app keys its configuration by policy name and carries the path as a value:
Quarkus writes `quarkus.http.auth.permission.roles1.paths=/api/*`, Symfony
writes `access_control: - {path: ^/admin}`, Resilience4j and ASP.NET Core name
the policy and never mention a path in the file at all. The systems that key
by path, NGINX `location` and a Caddy site block, are the routing table.
grelmicro is not: FastAPI, Starlette and Litestar own that.

## The snapshot cell

An ASGI middleware is built once and handed to the framework, which holds it
for the life of the process. `add_middleware` refuses to run after the stack
is built, so a live reconfigure cannot rebuild it.

So `asgi_middleware()` hands the middleware a `grelmicro._config.Live` cell
rather than the values. The component publishes a new snapshot into the cell
on reconfigure, and the middleware reads it once at the top of `__call__`:

```python
async def __call__(self, scope, receive, send):
    state = self._live.state
    config = state.config
```

Two plain attribute reads per request, both on slotted objects, and a single
assignment to publish. Both stay atomic on free-threaded builds, so neither
side takes a lock.

The cell holds one `_State`, never a field each. A cache that read its
`exclude` from one snapshot and its `vary_by_headers` from the next could
store an entry under the old key rules and serve it under the new ones, which
is the one way a response cache answers a caller with somebody else's
response. Every derived value the request path needs, a `frozenset` of
methods, lower-cased header names, the compiled path policies, lives on that
same `_State` and is rebuilt with it.

A helper called during a request takes the snapshot as a parameter rather than
reading its own, for the same reason.

A middleware built by hand owns a cell of its own, holding the one snapshot it
was constructed with. Both doors then read the same way, and the request path
has one shape rather than a branch.

## What stays out of the config

The config carries values. Everything else stays on the component and out of
reach of a mounted file:

| Component | Never in the config |
|---|---|
| `CachedResponses` | `cache`, `key`, `skip`, `namespace` |
| `ConditionalRequests` | `openapi`, read once when the schema is built |
| `IdempotentRequests` | `cache`, `key_maker`, `skip`, `namespace`, `openapi` |
| `RateLimitedRequests` | the limiters, `trusted`, `key` |

A `namespace` is part of every stored key, so changing it live would orphan
everything already stored rather than retune anything.

The limiters and the `Idempotency` are themselves `Reconfigurable`, under
`GREL_RATELIMITER_` and `GREL_IDEMPOTENCY_`. So the budget is live too, tuned
where it is held rather than where it is spent.

## What a reload may not do

**Live reload tunes what a request costs, never what it is protected by.**

`ConditionalRequests` and `IdempotentRequests` are configured at startup,
every field of them, because every one of theirs protects something.
Narrowing the reach lets a retry run twice or a write erase an update.
`fingerprint_body` off replays the first response to a different payload. A
lowered `max_body_size` stops a large response being stored, or carrying an
`ETag`. The header names, `require_key` and `reused_status` are fixed for a
second reason, that the OpenAPI schema states them and the schema is built
once.

The set is read off the config with
`frozenset(SomeConfig.model_fields)` rather than listed, so a field added
later is covered by the decision instead of becoming live because nobody
remembered to add it. A hand-written list is how `fingerprint_body` was
missed the first time.

`CachedResponses`, `RateLimitedRequests` and `AccessLog` are fully live,
because losing any of them costs latency or capacity rather than
correctness.

Every mature configurable system classifies its settings this way, in code
rather than per deployment: PostgreSQL's `pg_settings.context`, MySQL's
dynamic and static variables, Kafka's `read-only`, `per-broker` and
`cluster-wide` update modes. Two buckets is fewer than any of them.

It is an authorization boundary too. Write access to a ConfigMap is granted
far more widely than the right to ship an image, so the fields a mounted
file may change are the fields somebody holding only that permission may
change. `_IMMUTABLE_RECONFIGURE_FIELDS` is enforced in
`resolve_config_from_mapping`, so every adapter is covered, and the
construction path still reads them because that is the Deployment manifest,
shipped and reviewed with the image.

`RateLimitedRequests` keeps its reach live because its contract can be
stated as a superset: the schema documents `429` and the `RateLimit` fields
on every operation once the component is registered, which stays true
whichever paths are metered at the time. A rule that says what a client
*must send* has no superset form, which is why the other two are fixed.

### The route checks run again

`micro.install(app)` refuses a cache pattern naming a route that answers
something other than `GET`, or a read behind a security scheme, because a hit
answers before the route's own dependencies run.

Those checks run again in `CachedResponses._apply_reconfigure`, against the
app's own routes. Without that, a pattern arriving from a ConfigMap would walk
past a refusal the static path enforces, and live reload would open the exact
hole the install-time check closes. The rejected value is logged and skipped
by `reconfigure_all`, and the running configuration is kept.

## Two errors for one mistake

A set of path patterns given as a bare string is refused twice, in two
vocabularies.

On a component it is a settings value, so it raises `SettingsValidationError`
like every other bad component value, and the message never repeats what was
rejected.

On the middleware it is a hand-wired ASGI argument of the wrong type, so it
raises `TypeError`, which is what Python raises there and what a reader
hand-wiring a stack expects.

Both messages say the same thing, and `grelmicro._paths.BARE_STRING_MESSAGE`
is the one place it is written. It carries no example path on purpose:
`SettingsValidationError` removes the rejected value from the message it
renders, so an example that happened to equal what the caller passed would be
taken out of the very sentence offering it.

## Settled

| Decision | Assumption it rests on | Reopen when |
|---|---|---|
| An HTTP component is a single-instance component selected by path, never a named pattern | Its identity is the request it acts on, and it holds no runtime object a name would address | An HTTP component gains state a name has to reach |
| The per-endpoint view is a report, never a second config format | Code is the one source of truth, and a file can carry neither a callable nor a store | grelmicro owns the route table |
| A policy name is the address, a path never is | R3 makes the address an environment variable name, and `/` and `*` cannot be one | The address stops being an environment variable name |
| `include` and `exclude` are values a file may tune | They scope a component the code registered, and a file can never add one, or choose a store, a limiter, or a callable | A pattern starts choosing structure |
| The middleware reads a cell, not the values | A framework will not rebuild its middleware stack once it is serving | A framework gains a supported way to rebuild it |
| The cell holds one snapshot, never a field each | Two fields from two configurations can answer one caller with another's response | The fields stop being read together |
| Live reload tunes what a request costs, never what it is protected by | Editing a mounted file is a wider permission than shipping an image, and neither reviewed nor versioned with the code | A mounted source becomes as reviewed as a deploy |
| The OpenAPI schema is built once, from the app as installed | It is a published contract, and each replica polls on its own clock, so a live one would have two pods publishing two documents | The schema stops being served per replica |

## Related

- [Where a rule applies](../http/where.md): the user-facing half.
- [Configuration internals](config.md): `resolve_config` and the `Config` contract.
- [Live reconfiguration](reconfigure.md): the `Reconfigurable` mixin behind the swap.
