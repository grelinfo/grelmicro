# Access Log

One structured record per HTTP request, written by grelmicro.

Uvicorn already writes an access line, and it carries what uvicorn knows: the
socket peer, the request line, and the status. Behind an ingress the socket
peer is the ingress, which makes that line useless for the question people ask
an access log. This one carries what the app knows.

```python
--8<-- "log/access.py"
```

That is the whole setup. `micro.install(app)` adds the middleware, and every
request writes a record like this:

```json
{
  "time": "2026-09-04T15:02:00.115116+00:00",
  "level": "INFO",
  "msg": "GET /orders/7 200",
  "logger": "grelmicro.access",
  "trace_id": "e3c64457486c59d0ba764839dc404da5",
  "span_id": "92495292bd9b0710",
  "http.request.method": "GET",
  "url.path": "/orders/7",
  "url.query": "token=***&page=2",
  "url.scheme": "http",
  "http.route": "/orders/{order_id}",
  "http.response.status_code": 200,
  "client.address": "203.0.113.9",
  "user_agent.original": "curl/8.4",
  "network.protocol.version": "1.1",
  "http.server.request.duration": 0.000171
}
```

## The field names

They are [OpenTelemetry semantic
conventions](https://opentelemetry.io/docs/specs/semconv/http/http-spans/),
the same names the request span carries. A backend reads one vocabulary across
the log and the trace instead of a mapping between two, and `trace_id` and
`span_id` are on the record already, so a line joins the span it belongs to.

| Field | What it carries |
|---|---|
| `http.request.method` | The method. |
| `url.path` | The path the caller asked for, mount prefix and all. |
| `url.query` | The query string, redacted. Turn it off with `query=False`. |
| `url.scheme` | `http` or `https`. |
| `http.route` | The route template, when the framework records one. |
| `http.response.status_code` | The status the caller got. |
| `client.address` | The caller, resolved. |
| `user_agent.original` | The `User-Agent` header. Turn it off with `user_agent=False`. |
| `network.protocol.version` | `1.1`, `2`, and so on. |
| `http.server.request.duration` | Seconds, the unit the matching metric uses. |
| `error.type` | The exception a handler raised, when one did. |

No header is logged, so no header can leak. The query string is the one place
a URL carries a credential, and it goes through the same redaction the rest of
the library uses: `token=secret` reaches the sink as `token=***`.

## The caller, not the proxy

`client.address` is the address
[`ClientAddressMiddleware`](../security/clientip.md) resolved, which is why
the example registers it. Without it there is nothing to resolve from and the
record falls back to the transport peer, which behind an ingress is the
ingress. Which forwarded headers are believed is a trust decision, so it stays
where that decision is made.

## The level follows the answer

| Answer | Level |
|---|---|
| `5xx`, or a handler that raised | `ERROR` |
| `4xx` | `WARNING` |
| Anything else | `INFO` |
| A quiet path that answered | `DEBUG` |

Kubernetes polls `/livez`, `/readyz` and `/healthz` every few seconds for the
life of the pod, and Prometheus scrapes `/metrics` as often. At `INFO` they
crowd out every request a person wanted to read, so those four paths are quiet
by default: their record is written at `DEBUG`, where it is there when someone
goes looking and absent from the stream a person reads.

A probe that **fails** is logged like any other failure. A refused readiness
check is often the only line in the log saying the kubelet asked and was
turned away, so it is the one worth keeping.

```python
--8<-- "log/access_quiet.py"
```

`quiet=()` logs the probes like anything else. `exclude=` is the stronger
word: an excluded path writes nothing at any level, whatever it answered.
`include=` narrows to the paths you name. A pattern ending in `*` matches as a
prefix, and the match ignores the prefix a mount or a proxy adds, so a pattern
is the same in an app served at the root and one mounted under another.

## Uvicorn's access log

Registering `AccessLog()` silences `uvicorn.access` for as long as the app is
open. Two access logs on one stream is worse than either alone, and the one
grelmicro writes is the one carrying the resolved caller, the route and the
trace context.

Nothing else about uvicorn's logging changes: its error and startup records
still go through the [formatting](integrations.md) grelmicro applies. Without
`AccessLog()`, uvicorn's access log is untouched and
[`ProbeFilter`](filters.md) is still the way to drop probe noise from it.

## Sampling

There is none, on purpose. An access log you cannot count requests from is not
an access log, and a sampled line often has no span to join to, which is the
thing the record is for. To cut volume, name the paths: `quiet=` for what
should only speak up when it fails, `exclude=` for what should never be
written. For counting requests at any volume, use the [metrics](../metrics.md)
the components already emit.

## Route templates

`http.route` is the template, not the path that matched it, so a dashboard
groups `/orders/7` and `/orders/9` under `/orders/{order_id}`.

There is no standard ASGI key for it, so each framework is read the way it
records it. FastAPI and Litestar both do. Plain Starlette records no template,
so the field is left out rather than guessed from the values that filled it,
which is what OpenTelemetry's own ASGI instrumentation does with the same
problem.

| Framework | `http.route` |
|---|---|
| FastAPI | Yes |
| Litestar | Yes |
| Starlette | Left out |

## Without `install`

The middleware is pure ASGI, so an app on a framework `install` does not know
wraps itself with it:

```python
from grelmicro.log import AccessLogMiddleware

app = AccessLogMiddleware(app, quiet=("/healthz",))
```
