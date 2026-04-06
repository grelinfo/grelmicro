from typing import Any

from grelmicro.health import HealthRegistry

# Create the registry (auto-registers as the global singleton)
registry = HealthRegistry()


# Define health checkers using the HealthChecker protocol
class DatabaseChecker:
    @property
    def name(self) -> str:
        return "database"

    async def check(self) -> dict[str, Any] | None:
        # Replace with actual database ping
        return None


class RedisChecker:
    @property
    def name(self) -> str:
        return "redis"

    async def check(self) -> dict[str, Any] | None:
        # Return details (metrics, version, etc.)
        return {"latency_ms": 1.2}


# Register checkers
registry.add(DatabaseChecker())
registry.add(RedisChecker())
