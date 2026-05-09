from grelmicro.health import HealthChecks, HealthDetails

health = HealthChecks()


async def check_kafka() -> HealthDetails | None:
    return None


health.add("kafka", check_kafka, critical=True, timeout=2.0)
