from grelmicro.health import HealthChecks, HealthDetails

# Create the HealthChecks instance and register it with the Grelmicro
# app via `Grelmicro(uses=[health])` so the router can resolve it.
health = HealthChecks()


# Register checks with the @health.check(name) decorator
@health.check("database")
async def check_database() -> HealthDetails | None:
    # Return None on success, raise on failure
    return None


@health.check("redis")
async def check_redis() -> HealthDetails | None:
    # Return a dict to include details
    return {"latency_ms": 1.2}


# Optional dependency: mark non-critical so its failure doesn't
# take the instance out of the load balancer.
@health.check("external-api", critical=False)
async def check_external_api() -> HealthDetails | None:
    return None
