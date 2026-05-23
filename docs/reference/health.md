# Health

- **Start here**: [Health Checks guide](../health.md)
- **FastAPI integration**: [`health_router`](../health.md) for liveness, readiness, and health endpoints.

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
        - get_health_checks

::: grelmicro.health.fastapi
    options:
      members:
        - health_router
