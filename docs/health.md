# Health Checks

The `health` module provides a health check registry with concurrent checker execution and FastAPI integration for liveness, readiness, and aggregate health endpoints.

- **[HealthRegistry](#registry)**: Manages health checkers, runs them concurrently with per-checker timeouts, caches results for a short TTL.
- **[health_router](#fastapi-integration)**: FastAPI router with `/livez`, `/readyz`, and `/healthz` endpoints.

## Health Checker

A health checker is any class with a `name` property and an async `check` method. No base class to inherit from: the `HealthChecker` protocol uses structural subtyping.

```python
--8<-- "health/checker.py"
```

- Return `None`: healthy, with no details.
- Return a `dict`: healthy, with details such as latency, version, or connection count.
- Raise a `HealthError`: unhealthy. The exception message appears in the `error` field.
- Raise any other exception: unhealthy, with a generic `"Health check failed"` message. The full details are logged on the server to avoid leaking internal information.

## Registry

Create a `HealthRegistry` and register checkers:

```python
--8<-- "health/basic.py"
```

The registry auto-registers as the global singleton. The router resolves it automatically.

### Critical vs Non-Critical

Every checker is either **critical** (default) or **non-critical**. The distinction only affects the aggregated status:

| Scenario | Overall status | HTTP on `/readyz` |
|---|---|---|
| All checkers healthy | `healthy` | `200` |
| Non-critical checker failed | `degraded` | `200` |
| Critical checker failed | `unhealthy` | `503` |

```python
registry.add(DatabaseChecker())                        # critical (default)
registry.add(ExternalAPIChecker(), critical=False)     # non-critical
```

Non-critical checkers still run on every `/readyz` and `/healthz` request (the result is cached and shared between the two), and they appear in the `/healthz` body. Their failures do not remove the instance from the load balancer. Use this for optional dependencies (analytics sinks, recommendation APIs) that should not prevent your app from serving traffic.

### Timeout

Checkers that exceed the timeout are reported as unhealthy:

```python
--8<-- "health/timeout.py"
```

Timeout detection uses `anyio.move_on_after`. It correctly separates registry timeouts from a `TimeoutError` raised inside the checker itself, for example a socket timeout.

### Caching

The registry caches the last report for `cache_ttl` seconds (default `1.0`) and coalesces concurrent calls via single-flight. Without caching, each Kubernetes probe cycle (typically every 10s per replica, multiplied by liveness + readiness + load balancer health checks + dashboards) would re-run every checker, amplifying probe traffic onto your database.

```python
HealthRegistry(timeout=5.0, cache_ttl=1.0)   # default
HealthRegistry(cache_ttl=0)                  # disable caching
```

A 1.0s TTL is shorter than any realistic probe period, so probes still reflect reality within one cycle while bursts collapse to a single check run.

## FastAPI Integration

Add health endpoints to your FastAPI app:

```python
--8<-- "health/fastapi.py"
```

This creates three endpoints:

| Endpoint | Purpose | Success | Failure | Body |
|---|---|---|---|---|
| `GET /livez` | Liveness probe. Never runs checkers. | `200` | no response (timeout) | `ok` (`text/plain`) |
| `GET /readyz` | Readiness probe. Runs all registered checkers. | `200` | `503` | `ok` / `fail` (`text/plain`) |
| `GET /healthz` | Aggregate report for humans and dashboards. | `200` | `503` | JSON report |

All three also accept `HEAD` for cheap polling by uptime monitors and Prometheus-style scrapers. All responses set `Cache-Control: no-store`.

### Path choice

Paths use the Google z-pages convention (`/livez`, `/readyz`, `/healthz`) adopted by Kubernetes, etcd, and Spring Boot 3.x. The trailing `z` avoids collisions with application routes like `/health` in a healthcare app. Paths are configurable via `prefix` (see [URL prefix](#url-prefix)).

### Liveness vs readiness

- **Liveness** answers "is the process alive?". It never checks dependencies. If the process can respond, it is alive. If it cannot, the orchestrator (Kubernetes, Nomad, load balancer) will restart it. A failing dependency must never restart your pod.
- **Readiness** answers "can this instance serve traffic?". It runs all registered checkers concurrently. If any critical checker fails, the instance is removed from the load balancer until the next probe succeeds.

### Aggregate `/healthz` response

`/healthz` returns the full report:

```json
{
  "status": "unhealthy",
  "components": [
    {"name": "database", "status": "unhealthy", "critical": true, "error": "connection refused"},
    {"name": "redis", "status": "healthy", "critical": true, "error": null}
  ]
}
```

Probe endpoints (`/livez`, `/readyz`) intentionally return plaintext only. Body-based reporting is for humans and dashboards.

### Details

Checker details (metrics, version info, connection counts) are hidden by default for security. A verbose `/healthz` can leak framework versions, library versions, or internal hostnames, see for example [CVE-2026-29787](https://nvd.nist.gov/vuln/detail/CVE-2026-29787). Control visibility with the `show_details` parameter and the `?details` query parameter:

```python
from grelmicro.health.fastapi import health_router

router = health_router()                        # details hidden; ?details=true to show
router = health_router(show_details=True)       # details shown; ?details=false to hide
```

With details enabled:

```json
{
  "status": "healthy",
  "components": [
    {
      "name": "redis",
      "status": "healthy",
      "critical": true,
      "error": null,
      "details": {"latency_ms": 1.2, "version": "7.2"}
    }
  ]
}
```

For stricter production setups, gate `/healthz` behind authentication while leaving `/livez` and `/readyz` open (kubelet and most load balancers cannot carry credentials):

```python
from fastapi import Depends
from grelmicro.health.fastapi import health_router

router = health_router(
    show_details=True,
    healthz_dependencies=[Depends(require_admin)],   # applied only to /healthz
)
```

### URL prefix

Mount the health endpoints under a custom prefix:

```python
--8<-- "health/fastapi_prefix.py"
```

## Design

### Status vocabulary

```python
class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"     # at least one non-critical checker failed
    UNHEALTHY = "unhealthy"   # at least one critical checker failed
```

Three values, one enum, used for both components and the aggregate. The three-state shape matches ASP.NET Core HealthChecks (`Healthy`, `Degraded`, `Unhealthy`). grelmicro does not claim conformance with [draft-inadarei-api-health-check](https://datatracker.ietf.org/doc/html/draft-inadarei-api-health-check) (expired April 2022, never ratified) or with Spring Actuator's `UP`/`DOWN` vocabulary.

### Protocol-based

Checkers use structural subtyping (no inheritance required). Any object with `name: str` and `async check() -> dict[str, Any] | None` works:

```python
class MyChecker:
    @property
    def name(self) -> str:
        return "my-check"

    async def check(self) -> dict[str, Any] | None:
        return None
```

### Concurrent execution

All checkers run in parallel via an `anyio` task group. A slow checker does not block other checkers. Each checker has an individual timeout.

### Why three endpoints

The split is established: Kubernetes kube-apiserver exposes `/livez`, `/readyz`, `/healthz`; MicroProfile Health 4.0 mandates the split; Spring Boot Actuator 3.x promotes the same shape via `management.endpoint.health.probes.add-additional-paths`. Each endpoint serves a distinct audience:

- `/livez` serves the kubelet, which only needs to know whether to restart the pod.
- `/readyz` serves the load balancer, which only needs to know whether to route traffic.
- `/healthz` serves operators and dashboards, which need component-level detail.

Returning rich JSON on `/livez` and `/readyz` costs bytes, risks information leakage, and confuses the role of each probe. Returning empty `204 No Content` looks cleaner but is not compatible with the default AWS ALB matcher (`200`), so grelmicro returns `200` with a two-byte plaintext body.
