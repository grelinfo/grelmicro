from grelmicro.health import HealthChecks, HealthDetails

health = HealthChecks()


@health.check("external-api", critical=False)
async def check_external_api() -> HealthDetails | None:
    return None
