"""Shared test helpers for health checks."""

from typing import Any

import anyio

from grelmicro.health.errors import HealthError


class HealthyChecker:
    """A checker that always returns healthy (None)."""

    def __init__(self, name: str = "healthy") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> dict[str, Any] | None:
        """Return None (healthy, no details)."""
        return None


class HealthyCheckerWithDetails:
    """A checker that returns healthy with details."""

    def __init__(
        self, name: str = "detailed", details: dict[str, Any] | None = None
    ) -> None:
        """Initialize the checker."""
        self._name = name
        self._details = details or {"latency_ms": 1.5}

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> dict[str, Any] | None:
        """Return details dict."""
        return self._details


class UnhealthyChecker:
    """A checker that always raises."""

    def __init__(self, name: str = "unhealthy") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> dict[str, Any] | None:
        """Raise ConnectionError."""
        msg = "Connection refused"
        raise ConnectionError(msg)


class UnhealthyCheckerWithHealthError:
    """A checker that raises a HealthError subclass."""

    def __init__(self, name: str = "health-error") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> dict[str, Any] | None:
        """Raise a HealthError with a safe message."""
        msg = "Database connection pool exhausted"
        raise HealthError(msg)


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

    async def check(self) -> dict[str, Any] | None:
        """Sleep for the configured delay then return healthy."""
        await anyio.sleep(self._delay)
        return None
