from grelmicro.health import HealthChecks, HealthDetails

# Registry default: 2s per check (default is 5s)
health = HealthChecks(timeout=2.0)


# Per-check override: tight timeout for a flaky optional dep
@health.check("analytics", critical=False, timeout=0.5)
async def check_analytics() -> HealthDetails | None:
    return None
