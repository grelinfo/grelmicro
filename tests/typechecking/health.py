"""Type assertions for `HealthChecks`.

Guards the fix from #499: `@health.check` returns the decorated function
unchanged, so an async check stays awaitable at the call site instead of
degrading to `Any`.
"""

from typing import assert_type

from grelmicro.health import HealthChecks, HealthDetails

health = HealthChecks()


@health.check("db")
async def check_db() -> HealthDetails | None:
    """Check the database."""
    return None


@health.check("cache", critical=False)
def check_cache() -> HealthDetails | None:
    """Check the cache."""
    return None


async def call_checks() -> None:
    """Call the decorated checks, which keep their own signatures."""
    assert_type(await check_db(), HealthDetails | None)
    assert_type(check_cache(), HealthDetails | None)
