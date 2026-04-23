from grelmicro.health import HealthDetails, HealthRegistry

health = HealthRegistry()


@health.check("external-api", critical=False)
async def check_external_api() -> HealthDetails | None:
    return None
