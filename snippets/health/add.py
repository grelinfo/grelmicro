from grelmicro.health import HealthDetails, HealthRegistry

health = HealthRegistry()


async def check_kafka() -> HealthDetails | None:
    return None


health.add("kafka", check_kafka, critical=True, timeout=2.0)
