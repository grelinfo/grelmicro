from typing import Any

from grelmicro.health.errors import HealthError


# Any class with a `name` property and an async `check` method
# satisfies the HealthChecker protocol (structural subtyping)
class DatabaseChecker:
    @property
    def name(self) -> str:
        return "database"

    async def check(self) -> dict[str, Any] | None:
        # Return None on success (healthy, no details)
        return None


class RedisChecker:
    @property
    def name(self) -> str:
        return "redis"

    async def check(self) -> dict[str, Any] | None:
        # Return a dict to include details (e.g. metrics)
        return {"latency_ms": 1.2, "version": "7.2"}


class ExternalAPIChecker:
    @property
    def name(self) -> str:
        return "external-api"

    async def check(self) -> dict[str, Any] | None:
        # Raise HealthError to expose a specific message in the error field.
        # Other exceptions produce a generic "Health check failed" message.
        msg = "Connection refused"
        raise HealthError(msg)
