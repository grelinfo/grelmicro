from grelmicro.health import HealthChecks, HealthDetails
from grelmicro.health.errors import HealthError

health = HealthChecks()


@health.check("database")
async def check_database() -> HealthDetails | None:
    # Simulate a failure with a diagnostic payload
    msg = "connection pool exhausted"
    raise HealthError(msg, details={"active": 10, "idle": 0, "max": 10})
