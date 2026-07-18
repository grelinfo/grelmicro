"""Static typing samples for the `@health.check` decorator.

Runs as a pytest module so the imports execute, and is also picked up
by `uv run ty check` so the `assert_type` calls validate that the
decorator returns the function unchanged with its original signature.
A regression that widens the wrapped callable back to the
`SyncHealthCheckFunc | AsyncHealthCheckFunc` union fails ty (a direct
`await` on an async check would report `invalid-await`) even when all
runtime tests pass.
"""

from __future__ import annotations

from typing import assert_type

from grelmicro.health import HealthChecks
from grelmicro.health._types import HealthDetails

health = HealthChecks()


@health.check("database")
async def check_database() -> HealthDetails | None:
    """Async sample check."""
    return None


@health.check("disk")
def check_disk() -> HealthDetails | None:
    """Sync sample check."""
    return None


async def test_async_check_stays_directly_awaitable() -> None:
    """An async check keeps its coroutine return type through the decorator."""
    result = await check_database()
    assert_type(result, HealthDetails | None)


def test_sync_check_stays_a_plain_callable() -> None:
    """A sync check keeps its plain return type through the decorator."""
    result = check_disk()
    assert_type(result, HealthDetails | None)
