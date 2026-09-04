# Health

- **Start here**: [Health Checks guide](../health.md)
- **FastAPI integration**: [`health_router`](fastapi.md) for liveness, readiness, and health endpoints.
- **Every other framework**: `health_asgi` mounts the same endpoints as a pure-ASGI app, and [`OpsServer`](http.md) serves them on a port of its own.

::: grelmicro.health
    options:
      show_submodules: true
      members:
        - CheckResult
        - HealthCheckFunc
        - HealthDetails
        - HealthError
        - HealthChecks
        - HealthChecksConfig
        - HealthReport
        - HealthStatus
        - health_asgi
