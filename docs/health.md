# Health Checks

The `health` module provides a health check registry with concurrent execution and FastAPI integration for liveness, readiness, and aggregate health endpoints.

- **[HealthRegistry](#registry)**: Register check functions with a FastAPI-style decorator, run them concurrently with per-check timeouts and caching.
- **[health_router](#fastapi-integration)**: FastAPI router with `/livez`, `/readyz`, and `/healthz` endpoints.

## Health Check

A health check is a function returning `None` (healthy) or a `HealthDetails` dict (also healthy, with metrics attached). Raising an exception signals failure.

```python
--8<-- "health/checker.py"
```

- Return `None`: healthy, no details.
- Return a `HealthDetails` dict: healthy, with details. Values can be primitives, `datetime`, nested dicts, lists, or tuples.
- Raise `HealthError`: unhealthy. The exception message appears in the `error` field.
- Raise any other exception: unhealthy, with a generic `"Health check failed"` message. The traceback is logged server-side to avoid leaking internal information.

`HealthDetails` is a type alias for `dict[str, JSONEncodable]`. Both sync and async check functions are supported. Sync functions run in a worker thread via `asyncio.to_thread` so they never block the event loop.

## Registry

Create a `HealthRegistry` and register checks with the `@registry.check(name)` decorator:

```python
--8<-- "health/basic.py"
```

The registry auto-registers as the global singleton. The router resolves it automatically.

### Grelmicro app integration

Register the `HealthRegistry` with a `Grelmicro` app to lifecycle it alongside the rest of your modules. Same FastAPI-style explicit registration as `TaskManager`:

```python
from grelmicro import Grelmicro
from grelmicro.health import HealthRegistry

health = HealthRegistry()
micro = Grelmicro(includes=[health])

@health.check("redis")
async def redis_alive() -> None:
    ...

async with micro:
    report = await health.run()
```

Use `Grelmicro.include(item)` (or `includes=`) for entry-point components like `HealthRegistry` and `TaskManager`. The caller keeps the reference and uses it directly.

For imperative registration (without a decorator), use `registry.add(name, func)`:

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

The registry has a global default `timeout` (5.0 seconds). Per-check overrides are set on registration:

```python
--8<-- "health/per_check_timeout.py"
```

A slow non-critical check hits the timeout and is reported with `status: "error"` in the response body, but the aggregate stays `ok` and `/readyz` stays `200`.

Timeout detection uses `asyncio.timeout`. The wrapper distinguishes the registry-imposed timeout from a `TimeoutError` raised inside the check itself (for example a socket timeout).

### Caching

The registry caches each check's result for `cache_ttl` seconds (default `1.0`) and coalesces concurrent calls via single-flight per check. A given check runs at most once per TTL regardless of how many endpoints or concurrent requests are in flight. This prevents probe traffic from amplifying onto your database.

```python
--8<-- "health/caching.py"
```

### Returning Details with an Error

Raise `HealthError(message, details=...)` to attach a diagnostic payload to a failing check. The payload appears under `details` on the check entry, subject to `show_details`:

```python
--8<-- "health/error_details.py"
```

## FastAPI Integration

Add health endpoints to your FastAPI app:

```python
--8<-- "health/fastapi.py"
```

This creates three endpoints:

| Endpoint | Purpose | Success | Failure | Body |
|---|---|---|---|---|
| `GET /livez` | Liveness probe. Never runs checks. | `200` | no response (timeout) | empty |
| `GET /readyz` | Readiness probe. Runs critical checks only. | `200` | `503` | empty |
| `GET /healthz` | Aggregate JSON report for humans and dashboards. Runs all checks. | `200` | `503` | JSON `{status, checks}` |

All three also accept `HEAD`. All responses set `Cache-Control: no-store`. Probe endpoints return an empty body. The HTTP status code is the entire signal.

Paths follow the z-pages convention (`/livez`, `/readyz`, `/healthz`). The trailing `z` avoids collisions with application routes like `/health`.

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

Docker Compose example:

```yaml
services:
  app:
    image: myapp
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/livez"]
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

With details enabled, each check entry includes a `details` field:

```json
{
  "status": "ok",
  "checks": {
    "redis": {
      "status": "ok",
      "critical": true,
      "error": null,
      "details": {"latency_ms": 1.2, "version": "7.2"}
    }
  }
}
```

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

All selected checks run in parallel via an `asyncio.TaskGroup`. A slow check does not block other checks. Each check runs with its own timeout (falling back to the registry default).
