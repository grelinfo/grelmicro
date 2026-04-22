# Health Checks

The `health` module provides a health check registry with concurrent checker execution and FastAPI integration for liveness/readiness probes.

- **[HealthRegistry](#registry)**: Manages health checkers, runs them concurrently with per-checker timeouts.
- **[health_router](#fastapi-integration)**: FastAPI router with `/health/live` and `/health/ready` endpoints.

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

The registry auto-registers as the global singleton. The readiness endpoint resolves it automatically.

### Critical vs Non-Critical

By default, all checkers are **critical**: their failure causes the overall status to become `degraded` (HTTP 503). Register non-critical checkers with `critical=False`:

```python
# Critical (default): failure causes 503
registry.add(DatabaseChecker())

# Non-critical: reported but does not affect overall status
registry.add(ExternalAPIChecker(), critical=False)
```

Non-critical checkers still run and appear in the response, but their failures do not make the readiness probe fail. Use this setting for optional dependencies such as external APIs or analytics services that should not prevent your app from serving traffic.

### Timeout

Checkers that exceed the timeout are reported as unhealthy:

```python
--8<-- "health/timeout.py"
```

Timeout detection uses `anyio.move_on_after`. It correctly separates registry timeouts from a `TimeoutError` raised inside the checker itself, for example a socket timeout.

## FastAPI Integration

Add liveness and readiness endpoints to your FastAPI app:

```python
--8<-- "health/fastapi.py"
```

This creates two endpoints:

| Endpoint | Purpose | Response |
|---|---|---|
| `GET /health/live` | Liveness probe | Always `200 {"status": "healthy"}` |
| `GET /health/ready` | Readiness probe | `200` if all healthy, `503` if degraded |

### Readiness Response Example

```json
{
  "status": "degraded",
  "components": [
    {"name": "database", "status": "healthy", "error": null},
    {"name": "redis", "status": "unhealthy", "error": "Health check failed"}
  ]
}
```

### Details

Checker details (metrics, version info, etc.) are hidden by default for security. Control visibility with the `show_details` parameter and the `?details` query parameter:

```python
from grelmicro.health.fastapi import health_router

# Details hidden by default, shown with ?details=true
router = health_router()

# Details shown by default, hidden with ?details=false
router_with_details = health_router(show_details=True)
```

With details enabled, the response includes the `details` field:

```json
{
  "status": "healthy",
  "components": [
    {
      "name": "redis",
      "status": "healthy",
      "error": null,
      "details": {"latency_ms": 1.2, "version": "7.2"}
    }
  ]
}
```

### URL Prefix

Mount the health endpoints under a custom prefix:

```python
--8<-- "health/fastapi_prefix.py"
```

## Design

### Liveness vs Readiness

- **Liveness** answers "is the process alive?". It never checks dependencies. If the process can respond, it is alive. If it cannot, the orchestrator (Kubernetes, Nomad, load balancer) will restart it.
- **Readiness** answers "can this instance serve traffic?". It runs all registered checkers concurrently. If any checker fails or times out, the instance is marked as degraded and the orchestrator removes it from the load balancer.

### Protocol-Based

Checkers use structural subtyping (no inheritance required). Any object with `name: str` and `async check() -> dict[str, Any] | None` works:

```python
class MyChecker:
    @property
    def name(self) -> str:
        return "my-check"

    async def check(self) -> dict[str, Any] | None:
        return None
```

### Concurrent Execution

All checkers run in parallel via an `anyio` task group. A slow checker does not block other checkers. Each checker has an individual timeout.
