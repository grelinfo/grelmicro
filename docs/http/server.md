# Ops Server

A consumer reads from a queue. A scheduler fires jobs. Neither serves HTTP, and
Kubernetes still restarts the pod that stops answering `/livez`, and Prometheus
still needs a `/metrics` to scrape.

`OpsServer` gives that process a port of its own:

```python
--8<-- "http/ops_server.py"
```

That is the whole setup. No web framework, no ASGI server, no new dependency.
It serves what the app registers: the three health endpoints when a
`HealthChecks` is registered, and `/metrics` when a `Metrics` is.

| Endpoint | Served when | Answers |
|---|---|---|
| `GET /livez` | a `HealthChecks` is registered | `200`, empty body |
| `GET /readyz` | a `HealthChecks` is registered | `200` or `503`, empty body |
| `GET /healthz` | a `HealthChecks` is registered | the JSON report |
| `GET /metrics` | a `Metrics` is registered | the Prometheus exposition |

They answer exactly what the FastAPI router answers, because both render
through the same code. Read [Health Checks](../health.md) for the report and
`?exclude=`, and [Metrics](../metrics.md) for the exposition.

An app that registers neither says so at startup rather than listening on a
port that answers nothing:

```text
OpsServerError: OpsServer has nothing to serve. Register a HealthChecks, a
Metrics, or both: Grelmicro(uses=[HealthChecks(), OpsServer()]).
```

## The Kubernetes side

The port is the one your probes point at:

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8080 }
readinessProbe:
  httpGet: { path: /readyz, port: 8080 }
```

The default binds every interface, IPv4 and IPv6, because the kubelet reaches
a pod on its pod IP and a dual-stack cluster may use either. Set
`host="127.0.0.1"` to keep the port on loopback, for a sidecar that scrapes
from inside the pod.

## Configuration

| Parameter | Default | What it decides |
|---|---|---|
| `port` | `8080` | The port it listens on. |
| `host` | every interface | The address it binds. |
| `show_details` | `False` | Whether `/healthz` carries each check's `details`. |
| `request_timeout` | `10.0` | Seconds one request may take, first byte to last. |
| `shutdown_timeout` | `5.0` | Seconds in-flight requests get to finish on shutdown. |
| `max_connections` | `32` | Connections served at once. |

Each one reads from the environment under `GREL_OPS_`, so a deployment moves
the port without touching the code:

```bash
GREL_OPS_PORT=9100
```

Two servers on one app take a name each, and a named one reads
`GREL_OPS_{NAME}_`:

```python
micro = Grelmicro(uses=[health, OpsServer(), OpsServer(name="admin", port=8081)])
```

## Where to register it

Register it first in `uses=[...]`. Components close in reverse order, so the
one registered first closes last, and the probes keep answering while the rest
of the app drains. On shutdown it stops accepting immediately, lets in-flight
requests finish within `shutdown_timeout`, and cancels what is still running.

## What it is, and what it is not

It is a small HTTP/1.1 server on the standard library. It reads a request line
and its headers, answers, and closes. It reads no request body, keeps no
connection alive, and speaks no TLS.

That is enough for a kubelet, a load balancer, and a Prometheus scrape, and it
is deliberately not enough for anything else. Give it the pod network, not an
ingress. A request it cannot read gets the status that says why:

| Answer | When |
|---|---|
| `400` | The request line or a header is malformed. |
| `404` | The path is not one it serves. |
| `405` | The method is not `GET` or `HEAD`. |
| `408` | The request stopped mid-way and `request_timeout` elapsed. |
| `413` | The request carries a body larger than 8 KiB. |
| `431` | A header line, or the number of headers, is over the limit. |
| `501` | The request uses chunked framing. |
| `503` | `max_connections` are already in flight. |

## Mounting the endpoints instead

A process that already runs an ASGI framework does not need a second port.
Mount the endpoints in the app it already serves:

```python
--8<-- "health/asgi.py"
```

`health_asgi()` and `metrics_asgi()` are pure-ASGI apps, so they mount in
Starlette, Litestar, or anything else that speaks ASGI, and they answer exactly
what `OpsServer` answers. On FastAPI, prefer
[`health_router()`](../health.md#fastapi-integration): it serves the same
endpoints and adds the OpenAPI schema and the `Depends` gates.

Read [Frameworks](../frameworks.md) for what runs where.
