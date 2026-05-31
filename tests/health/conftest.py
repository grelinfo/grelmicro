"""Shared test helpers for health checks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from grelmicro.health.errors import HealthError


@pytest.fixture(autouse=True)
def _isolate_health_logger() -> Iterator[None]:
    """Give each test a pristine ``grelmicro.health`` logger.

    The log-capture tests rely on `caplog` seeing records propagate to
    the root logger. A co-resident test in the same xdist worker can
    leave global logging state behind (a raised level, ``propagate``
    turned off, a leaked filter or handler, or a ``logging.disable``)
    that silently swallows those records. Reset the logger and the
    global disable level around every test so capture is deterministic.
    """
    logger = logging.getLogger("grelmicro.health")
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_filters = logger.filters[:]
    saved_handlers = logger.handlers[:]
    saved_disable = logging.root.manager.disable
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    logger.filters.clear()
    logger.handlers.clear()
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.filters[:] = saved_filters
        logger.handlers[:] = saved_handlers
        logging.disable(saved_disable)


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
