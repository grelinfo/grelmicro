"""Shared test helpers for health checks."""

import anyio

from grelmicro.health._models import HealthStatus


class HealthyChecker:
    """A checker that always returns HEALTHY."""

    def __init__(self, name: str = "healthy") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> HealthStatus:
        """Return HEALTHY."""
        return HealthStatus.HEALTHY


class UnhealthyChecker:
    """A checker that always raises."""

    def __init__(self, name: str = "unhealthy") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> HealthStatus:
        """Raise ConnectionError."""
        msg = "Connection refused"
        raise ConnectionError(msg)


class SlowChecker:
    """A checker that takes longer than the timeout."""

    def __init__(self, name: str = "slow", delay: float = 10.0) -> None:
        """Initialize the checker."""
        self._name = name
        self._delay = delay

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> HealthStatus:
        """Sleep for the configured delay then return HEALTHY."""
        await anyio.sleep(self._delay)
        return HealthStatus.HEALTHY
