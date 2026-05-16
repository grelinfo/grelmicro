from grelmicro.health import HealthChecks, HealthDetails

health = HealthChecks()


@health.check("slow-api", critical=False, timeout=0.5)
async def check_slow_api() -> HealthDetails | None:
    return None
