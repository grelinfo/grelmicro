from grelmicro.health import HealthDetails, HealthRegistry

# Registry default: 2s per check (default is 5s)
health = HealthRegistry(timeout=2.0)


# Per-check override: tight timeout for a flaky optional dep
@health.check("analytics", critical=False, timeout=0.5)
async def check_analytics() -> HealthDetails | None:
    return None
