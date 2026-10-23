# Health Checks

A health checks manager with concurrent execution. Use it to expose liveness, readiness, and aggregate health endpoints.

- **[HealthChecks](#quick-start)**: Register check functions with a FastAPI-style decorator, run them concurrently with per-check timeouts and caching.
- **[OpsServer](#serve-the-endpoints)**: Serve `/livez`, `/readyz`, `/healthz` and `/metrics` on a port of their own. The recommended way.
- **[health_router](#fastapi-integration)**: FastAPI router that serves the same endpoints on the app port.

## Quick start

Create a `HealthChecks` and register checks with the `@health.check(name)` decorator:

```python
--8<-- "health/basic.py"
```

Register the instance with a `Grelmicro` app (see below) so [`OpsServer`](#serve-the-endpoints) or the router can resolve it, or pass it explicitly via `health_router(health)`.

## Health Check

A health check is a function returning `None` (healthy) or a `HealthDetails` dict (also healthy, with metrics attached). Raising an exception signals failure.

```python
--8<-- "health/checker.py"
```

- Return `None`: healthy, no details.
- Return a `HealthDetails` dict: healthy, with details. Values can be primitives, `datetime`, nested dicts, lists, or tuples.
- Raise `HealthError`: unhealthy. The exception message appears in the `error` field.
- Raise any other exception: unhealthy, with an `error` field formatted as `"{ExceptionType}: {message}"` (for example `"ValueError: bad input"`). The traceback is logged server-side to avoid leaking internal information.

`HealthDetails` is a type alias for `dict[str, JSONEncodable]`. Both sync and async check functions are supported. Sync functions run in a worker thread via `asyncio.to_thread` so they never block the event loop.

!!! warning "The check decorator goes on top"

    `@health.check` registers the function it is handed and returns that
    same one. A decorator written below it wraps the module-level name,
    while the registry keeps holding the original, so it applies to direct
    calls and never to a probe. Put `@health.check` on top:

    ```python
    @health.check("database")
    @retry(when=TimeoutError, attempts=3)
    async def check_db() -> None: ...
    ```

    Written the other way round, `@retry` refuses with a `TypeError` that
    names the right order.

    A check registered as a bound method, with `health.add("db", svc.check)`,
    guards that object's method and the class function it runs through.
    Another instance of the same class is left alone. Wrapping a method
    loses the object it was bound to, so a decorator that copies the name
    with `functools.wraps` is not refused, because refusing it would refuse
    every other instance too. Put the registering decorator on top and the
    question does not arise.

### Grelmicro app integration

Register the `HealthChecks` with a `Grelmicro` app to lifecycle it alongside the rest of your modules. Same FastAPI-style explicit registration as `Tasks`:

```python title="fragment"
from grelmicro import Grelmicro
from grelmicro.health import HealthChecks

health = HealthChecks()
micro = Grelmicro(uses=[health])

@health.check("redis")
async def redis_alive() -> None:
    ...

async with micro:
    report = await health.run()
```

Use `Grelmicro.use(item)` (or `uses=`) for entry-point components like `HealthChecks` and `Tasks`. The caller keeps the reference and uses it directly.

For imperative registration (without a decorator), use `health.add(name, func)`:

```python
--8<-- "health/add.py"
```

### Critical vs Non-Critical

By default, all checks are **critical**: a failure flips the aggregate to `error` and returns `503` on `/readyz` and `/healthz`. Pass `critical=False` for optional dependencies:

```python
--8<-- "health/non_critical.py"
```

| Scenario | Aggregate status on `/healthz` | `/readyz` | `/healthz` |
|---|---|---|---|
| All critical pass | `ok` | `200` | `200` |
| Non-critical failed | `ok` | `200` | `200` |
| Critical failed | `error` | `503` | `503` |

Non-critical checks do not pull the instance from the load balancer. They are not run on `/readyz` (which runs critical only). Their status appears per-check in the `/healthz` body so operators and dashboards can see degraded dependencies without triggering traffic removal.

### Timeout

Checks that exceed their timeout are reported as failed:

```python
--8<-- "health/timeout.py"
```

`HealthChecks` has a global default `timeout` (5.0 seconds). Per-check overrides are set on registration:

```python
--8<-- "health/per_check_timeout.py"
```

A slow non-critical check hits the timeout and is reported with `status: "error"` in the response body, but the aggregate stays `ok` and `/readyz` stays `200`.

Timeout detection uses `asyncio.timeout`. The wrapper distinguishes the configured timeout from a `TimeoutError` raised inside the check itself (for example a socket timeout).

### Caching

`HealthChecks` caches each check's result for `cache_ttl` (default 1 second, in whole seconds or as a `timedelta`) and coalesces concurrent calls via single-flight per check. A given check runs at most once per TTL regardless of how many endpoints or concurrent requests are in flight. This prevents probe traffic from amplifying onto your database.

```python
--8<-- "health/caching.py"
```

### Returning Details with an Error

Raise `HealthError(message, details=...)` to attach a diagnostic payload to a failing check. The payload appears under `details` on the check entry, subject to `show_details`:

```python
--8<-- "health/error_details.py"
```

## Provider Readiness Checks

Every connection provider ships a built-in readiness probe. Redis and Valkey run `PING`, Postgres and SQLite run `SELECT 1`. These are cheaper and more honest than a borrowed lock round trip, and you no longer hand-write one check per backend.

Register a provider's check with `add_provider`. It registers under `provider:{short_name}`, for example `provider:redis`:

```python
--8<-- "health/provider_check.py"
```

The check is **critical** by default, so an unreachable backend fails `/readyz`. Pass `critical=False` for a degradable dependency such as a cache. Pass `name=` to disambiguate two providers of the same vendor, which registers `provider:{name}` instead.

To register a check for every provider on the app at once, build the `HealthChecks` with `auto_health=True`:

```python
--8<-- "health/auto_health.py"
```

On startup this discovers every active provider, including ones a Component borrows without listing, and registers a critical `provider:{short_name}` check for each. A provider with no built-in probe is skipped. It is off by default, so checks appear only when you ask for them. When two providers share a `short_name`, the second is skipped with a warning: register it explicitly with `add_provider(provider, name=...)` to give it a distinct name.

## Circuit Breakers

Register a check on a [circuit breaker](resilience/circuit-breaker.md) with `add_breaker`. It registers under `circuitbreaker:{breaker.name}`, for example `circuitbreaker:inverter`:

```python
--8<-- "health/breaker_check.py"
```

The check is **non-critical** by default. An open breaker usually means a shared dependency is down for every replica, so failing readiness would take every replica out of rotation at once. Pass `critical=True` when the service is useless without that dependency. Pass `name=` when the breaker name is not a valid check name.

Each run reads the state from the breaker's backend, so a replica that made no call still sees a breaker another replica opened.

| State | Check status | Reported |
| --- | --- | --- |
| `CLOSED` | `ok` | nothing |
| `HALF_OPEN`, `FORCED_CLOSED` | `ok` | `{"state": "HALF_OPEN"}` in the details |
| `OPEN` | `error` | `circuit breaker open, next probe in 12s`, details `{"state": "OPEN", "retry_after": 12}` |
| `FORCED_OPEN` | `error` | `circuit breaker forced open until reset`, details `{"state": "FORCED_OPEN"}` |

## Serve the Endpoints

Register an `OpsServer` next to your app. It serves the probes and `/metrics` on a port of their own, so they never share the port your users reach:

```python
--8<-- "health/ops_server.py"
```

It listens on port 8080 by default. Pass `port=` or set `GREL_OPS_PORT` to move it. Point the probes and the Prometheus scrape at that port, and keep it off the internet: read [Secure the ops endpoints](deployment.md#secure-the-ops-endpoints). It works the same next to FastAPI, another ASGI framework, or [no web framework at all](#without-a-web-framework). [Ops Server](http/server.md) covers the configuration.

## FastAPI Integration

Some platforms probe only the port they route traffic to. There, serve the endpoints from the app itself:

```python
--8<-- "health/fastapi_app.py"
```

!!! warning "The app port is the public port"
    Whatever the router serves, every caller who reaches the app reaches it too. Behind a reverse proxy, that is the internet. `/healthz` names every check and reports each failing check's error string. Gate it with `healthz_dependencies` and keep `show_details` off, as shown in [Verbose Details](#verbose-details).

This creates three endpoints:

| Endpoint | Purpose | Success | Failure | Body |
|---|---|---|---|---|
| `GET /livez` | Liveness probe. Runs no check per request, and reflects the [liveness checks](#catch-a-stuck-worker) when set. | `200` | `503`, or no response (timeout) | empty |
| `GET /readyz` | Readiness probe. Runs critical checks only. | `200` | `503` | empty |
| `GET /healthz` | Aggregate JSON report for humans and dashboards. Runs all checks. | `200` | `503` | JSON `{status, checks}` |

`/livez` never checks a dependency. Kubernetes restarts the container whose liveness probe fails, so a check on a database there would restart every replica when the database goes down. [Catch a stuck worker](#catch-a-stuck-worker) shows what `/livez` catches and how to catch the rest.

All three also accept `HEAD`. All responses set `Cache-Control: no-store`. Probe endpoints return an empty body. The HTTP status code is the entire signal.

Paths follow the z-pages convention (`/livez`, `/readyz`, `/healthz`). The trailing `z` avoids collisions with application routes like `/health`.

`micro.install(app)` mounts no route. Register an `OpsServer`, or include the router yourself. A registered `HealthChecks` that nothing serves is reported at startup with the [`health-not-served`](diagnostics.md#health-not-served) warning.

### OpenAPI Schema

The three endpoints stay out of the OpenAPI schema. They answer exactly the same, and `/openapi.json` and the docs pages name neither the probes nor the shape of the report. An orchestrator does not read your schema, and a generated client has no use for a probe.

Pass `include_in_schema=True` to publish them, with the `/healthz` response model and its `503` response. `/livez` and `/readyz` are documented as returning no body, because that is what they return:

```python
--8<-- "health/openapi.py"
```

This decides what the schema publishes, not what is reachable. To keep `/healthz` from answering at all, gate it with `healthz_dependencies`. To close a probe off, leave it off your ingress.

### Report Body

`/healthz` reports the aggregate status and one entry per check. A check that passed carries `status` and `critical` alone:

```json
{
  "status": "error",
  "checks": {
    "store": {"status": "ok", "critical": true},
    "analytics": {"status": "ok", "critical": false},
    "database": {"status": "error", "critical": true, "error": "connection refused"}
  }
}
```

A field that has nothing to report is left out rather than sent as `null`. `error` appears only on a failing check, and `details` only when the check returned some and `show_details` allows them. Read an absent `error` as "this check passed".

### Using with Docker, Compose, and other Orchestrators

Different orchestrators consume different probes. grelmicro exposes all three endpoints, pick the ones that fit:

| Orchestrator | Uses |
|---|---|
| Kubernetes, OpenShift | `livenessProbe` → `/livez`, `readinessProbe` → `/readyz`, `startupProbe` → `/livez` |
| Docker (`HEALTHCHECK`), Docker Compose, Docker Swarm | single healthcheck → `/livez` (restart on failure) |
| Nomad (`check` stanza) | `/livez` for liveness, `/readyz` for routing |
| systemd (`WatchdogSec`) | `/livez` (restart on failure) |
| Reverse proxies (Traefik, nginx, HAProxy, Envoy), load balancers (AWS ALB, GCP LB) | `/readyz` for upstream health |
| Dashboards, uptime monitors (Prometheus, Pingdom) | `/healthz` for full report |

Docker Compose example, with the probes on the `OpsServer` port:

```yaml
services:
  app:
    image: myapp
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/livez"]
      interval: 10s
      timeout: 2s
      retries: 3
```

Docker and Docker Swarm only model a single healthcheck per container, so point it at `/livez`. Route readiness through a reverse proxy that probes `/readyz`.

### Exclude Checks

Both `/readyz` and `/healthz` accept an `?exclude` query parameter with a comma-separated list of check names. Useful for temporarily muting a known-flaky check without redeploy:

```text
GET /readyz?exclude=analytics,recommendations
GET /healthz?exclude=analytics
```

Excluded checks are not run and do not appear in the response.

### Verbose Details

Each check can return verbose metadata (versions, internal hostnames, pool stats, latencies). Exposing it publicly on `/healthz` leaks infrastructure fingerprints to anyone who can reach the endpoint, so by default grelmicro strips every `details` field from the response.

The gates below apply to `health_router()` on the app port. `OpsServer` takes `show_details=True` or `False` and nothing else, because the network in front of it is what keeps it private.

The `show_details` parameter controls visibility with three forms:

| `show_details` | Who sees details | Use when |
|---|---|---|
| `False` (default) | nobody | Safe default for a public `/healthz` |
| `True` | everyone | `/healthz` is on a private network only |
| `Depends(fn)` | requests for which `fn()` returns `True` | Public `/healthz`, admin-only details |

With `show_details=Depends(fn)`, `fn` is wired into FastAPI's dependency-injection graph. Return `True` to include details for that request, `False` to strip them. Everything FastAPI supports works: `Depends` sub-dependencies, `Security`, `Request` injection, async functions, `yield` cleanup. Returning `False` strips details but the endpoint still returns `200`/`503` with status and check names. This way, uptime monitors without credentials still get actionable aggregate status while admin tools with credentials get the full payload. Raising `HTTPException` blocks the endpoint, so prefer returning `False` for a soft strip.

```python
--8<-- "health/show_details.py"
```

With details enabled, a check that returned some includes a `details` field:

```json
{
  "status": "ok",
  "checks": {
    "redis": {
      "status": "ok",
      "critical": true,
      "details": {"latency_ms": 1.2, "version": "7.2"}
    }
  }
}
```

!!! warning "The `error` field is always returned"
    `show_details` strips only the `details` field. Each failing check still reports its `error` string, even on a public `/healthz`. Keep check error messages safe for the endpoint audience: raise `HealthError` with a short, non-sensitive message and put diagnostics in `details`, which stays hidden by default. To hide error strings too, gate the whole endpoint with `healthz_dependencies`.

#### `show_details` vs `healthz_dependencies`

Two independent gates sit in front of `/healthz`:

| Parameter | Failure effect | Typical use |
|---|---|---|
| `healthz_dependencies` | Blocks the entire endpoint (`401`/`403`) | Keep `/healthz` entirely private |
| `show_details=Depends(fn)` | When `fn()` returns `False`, strips the `details` field. Endpoint still returns `200`/`503` | Keep aggregate status public, hide verbose metadata |

For stricter setups, gate the whole `/healthz` endpoint behind authentication while leaving `/livez` and `/readyz` open (most orchestrators and load balancers cannot carry credentials):

```python
--8<-- "health/healthz_auth.py"
```

### URL Prefix

Mount the health endpoints under a custom prefix:

```python
--8<-- "health/fastapi_prefix.py"
```

## Catch a stuck worker

A worker is stuck when its event loop is blocked by a long synchronous call, or when coroutines or threads deadlock while the loop still runs. With one worker per container, a blocked loop stops answering `/livez`, so the probe times out and the container restarts. A deadlock that leaves the loop free still answers `200`, and with several workers per container an idle worker answers the probe for a stuck one.

Set `Liveness` to catch both, in every setup:

```python
--8<-- "health/liveness.py"
```

- **The loop watchdog.** With `stall_timeout`, a thread checks that the loop still runs callbacks, four times per `stall_timeout` and at most once a second. Past `stall_timeout`, it logs where the loop thread is stuck and exits the process. It pauses while the app opens or closes.
- **Liveness checks.** A check with `liveness=True` runs every `interval` seconds (10 by default), never on `/readyz` or `/healthz`. `/livez` answers `503` from the first failed round. After `failure_threshold` failed rounds in a row, the worker stops itself with `SIGTERM`. If it has not exited `shutdown_timeout` seconds later (30 by default), it exits at once. A check with `critical=False` is logged when it fails, like a readiness check, and never counts as a failed round.

Either way the worker exits, and whatever runs it starts another: Kubernetes restarts the container, Gunicorn replaces the worker, and so does the `uvicorn --workers` supervisor.

!!! warning "Look inside the process only"
    A liveness check that reads a database or another service restarts every replica when that service goes down. Check what only this worker knows, such as whether its consumer made progress.

## Without a Web Framework

A worker, a consumer, or a scheduler serves no HTTP, and Kubernetes still
probes it. `OpsServer` gives it a port of its own, with no framework and no
new dependency:

```python
from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.http import OpsServer

micro = Grelmicro(uses=[HealthChecks(auto_health=True), OpsServer(port=8080)])
```

`GET /livez`, `/readyz` and `/healthz` answer on 8080, exactly as the FastAPI
router answers them. Read [Ops Server](http/server.md).

On another ASGI framework, mount the endpoints in the app you already serve:

```python
--8<-- "health/asgi.py"
```

`health_asgi()` is a pure-ASGI app. It mounts in Starlette, Litestar, or
anything else that speaks ASGI, and it takes the same `prefix` and
`show_details` the router takes. Mounted at `""` it matches every path, so it
goes after the routes of your own. It has no `Depends`, so gate it with your own
middleware where the router would take `healthz_dependencies`.

| You run | Recommended | On the app port instead |
|---|---|---|
| FastAPI | `OpsServer()` | `health_router()`, with the OpenAPI schema and `Depends` gates |
| Another ASGI framework | `OpsServer()` | `health_asgi()`, gated by your own middleware |
| No web framework | `OpsServer()` | None, there is no app port |

## Design

### Why Three Endpoints

Each endpoint serves a different audience:

| Endpoint | Audience | Answers |
|---|---|---|
| `/livez` | Orchestrator (Kubernetes, Docker, Nomad, systemd) | Is the process alive? Should it be restarted? |
| `/readyz` | Load balancer, reverse proxy, service mesh | Can this instance serve traffic? |
| `/healthz` | Operators, dashboards, uptime monitors | What is the state of each component? |

Liveness never checks dependencies. A failing database must never restart your container. Readiness runs all critical checks concurrently. If any fails, the instance is removed from the load balancer. Aggregate also runs non-critical checks and returns a JSON report for humans. Probe bodies stay empty to keep the wire minimal and the signal unambiguous. The HTTP status code is the only thing orchestrators read.

### Status Vocabulary

Binary status, used for both components and the aggregate:

| Status | Meaning |
|---|---|
| `ok` | The check passed. At the aggregate level: every critical check passed. |
| `error` | The check failed. At the aggregate level: at least one critical check failed. |

Non-critical failures produce `status: "error"` on the individual check but do not flip the aggregate. The aggregate only goes to `error` when at least one **critical** check fails.

### Function-Based API

Checks are plain functions. No base class to inherit from.

```python
--8<-- "health/checker.py"
```

This mirrors FastAPI's own `@app.get("/path")` routing style and keeps grelmicro small. Return types are statically checked. `JSONEncodable` (recursive) includes primitives, `datetime`, nested `dict` / `list` / `tuple`, and any `Mapping` subclass. Type checkers (mypy, ty) catch non-serializable returns like `bytes` or custom objects at authoring time.

### Concurrent Execution

All selected checks run in parallel via an `asyncio.TaskGroup`. A slow check does not block other checks. Each check runs with its own timeout (falling back to the default).
