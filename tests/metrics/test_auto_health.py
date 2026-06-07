"""Auto-instrumentation tests for the health component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.health import HealthChecks
from grelmicro.health.errors import HealthError

if TYPE_CHECKING:
    from tests.metrics.conftest import MetricsHarness


async def test_health_emits_up_and_duration(
    metrics_reader: MetricsHarness,
) -> None:
    """A healthy check emits up=1 and a duration with outcome=success."""
    checks = HealthChecks(cache_ttl=0)

    @checks.check("redis")
    async def _ok() -> None:
        return None

    await checks.run()

    up = metrics_reader.points("grelmicro.health.check.up")
    assert up[0][0] == 1
    assert up[0][1] == {"check.name": "redis", "critical": True}

    duration = metrics_reader.points("grelmicro.health.check.duration")
    assert duration[0][1]["check.name"] == "redis"
    assert duration[0][1]["outcome"] == "success"


async def test_health_emits_down_on_failure(
    metrics_reader: MetricsHarness,
) -> None:
    """A failing check emits up=0 and outcome=error."""
    checks = HealthChecks(cache_ttl=0)

    @checks.check("db", critical=False)
    async def _bad() -> None:
        raise HealthError

    await checks.run()

    up = metrics_reader.points("grelmicro.health.check.up")
    assert up[0][0] == 0
    assert up[0][1] == {"check.name": "db", "critical": False}
    duration = metrics_reader.points("grelmicro.health.check.duration")
    assert duration[0][1]["outcome"] == "error"


async def test_health_metrics_noop_when_off() -> None:
    """Health checks run without error when no Metrics component is active."""
    checks = HealthChecks(cache_ttl=0)

    @checks.check("svc")
    async def _ok() -> None:
        return None

    report = await checks.run()
    assert report["status"] == "ok"
