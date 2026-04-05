from typing import Any


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
        # Raise any exception to signal unhealthy.
        # The exception message becomes the error field.
        msg = "Connection refused"
        raise ConnectionError(msg)
