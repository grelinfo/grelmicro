"""Shared test helpers for health checks."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from grelmicro.health.errors import HealthError


def healthy() -> Callable[[], Awaitable[dict[str, Any] | None]]:
    """Return a check function that always reports healthy (None)."""

    async def _check() -> dict[str, Any] | None:
        return None

    return _check


def healthy_with_details(
    details: dict[str, Any] | None = None,
) -> Callable[[], Awaitable[dict[str, Any] | None]]:
    """Return a check function that reports healthy with details."""
    payload = details if details is not None else {"latency_ms": 1.5}

    async def _check() -> dict[str, Any] | None:
        return payload

    return _check


def unhealthy() -> Callable[[], Awaitable[dict[str, Any] | None]]:
    """Return a check that raises ``ConnectionError`` (generic failure)."""

    async def _check() -> dict[str, Any] | None:
        msg = "Connection refused"
        raise ConnectionError(msg)

    return _check


def unhealthy_with_health_error() -> Callable[
    [], Awaitable[dict[str, Any] | None]
]:
    """Return a check that raises a HealthError with a safe message."""

    async def _check() -> dict[str, Any] | None:
        msg = "Database connection pool exhausted"
        raise HealthError(msg)

    return _check


def slow(
    delay: float = 10.0,
) -> Callable[[], Awaitable[dict[str, Any] | None]]:
    """Return a check that sleeps for ``delay`` seconds then returns healthy."""

    async def _check() -> dict[str, Any] | None:
        await asyncio.sleep(delay)
        return None

    return _check


class Counting:
    """Callable health check that counts invocations."""

    def __init__(self) -> None:
        """Initialize counter."""
        self.calls = 0

    async def __call__(self) -> dict[str, Any] | None:
        """Record the call and return healthy."""
        self.calls += 1
        return None


class SlowCounting:
    """Callable health check that sleeps and counts invocations."""

    def __init__(self, *, delay: float) -> None:
        """Initialize counter and delay."""
        self._delay = delay
        self.calls = 0

    async def __call__(self) -> dict[str, Any] | None:
        """Record the call, sleep, return healthy."""
        self.calls += 1
        await asyncio.sleep(self._delay)
        return None
