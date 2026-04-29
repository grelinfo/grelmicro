from grelmicro.health import HealthDetails, HealthRegistry
from grelmicro.health.errors import HealthError

health = HealthRegistry()


# Decorator form: register an async function under a name
@health.check("database")
async def check_database() -> HealthDetails | None:
    # Return None on success (healthy, no details)
    return None


@health.check("redis")
async def check_redis() -> HealthDetails | None:
    # Return a dict to include details (e.g. metrics)
    return {"latency_ms": 1.2, "version": "7.2"}


@health.check("external-api", critical=False)
async def check_external_api() -> HealthDetails | None:
    # Raise HealthError to expose a specific message in the error field.
    # Other exceptions produce a generic "Health check failed" message.
    msg = "Connection refused"
    raise HealthError(msg)
