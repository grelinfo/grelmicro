"""Tests for Health Check Registry."""

import time

import anyio
import pytest

from grelmicro.health._models import HealthStatus, OverallStatus
from grelmicro.health._registry import HealthRegistry
from grelmicro.health.errors import HealthCheckTimeoutError

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


# --- Test helpers ---


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


# --- Tests ---


async def test_empty_registry_is_healthy() -> None:
    """Test that a registry with no checkers returns HEALTHY."""
    registry = HealthRegistry()

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert report["components"] == []


async def test_single_healthy_checker() -> None:
    """Test registry with one healthy checker."""
    registry = HealthRegistry()
    registry.add(HealthyChecker("db"))

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "db"
    assert report["components"][0]["status"] == HealthStatus.HEALTHY
    assert report["components"][0]["detail"] is None


async def test_single_unhealthy_checker() -> None:
    """Test registry with one failing checker."""
    registry = HealthRegistry()
    registry.add(UnhealthyChecker("redis"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "redis"
    assert report["components"][0]["status"] == HealthStatus.UNHEALTHY
    assert report["components"][0]["detail"] == "Connection refused"


async def test_mixed_healthy_and_unhealthy() -> None:
    """Test registry with both healthy and unhealthy checkers."""
    registry = HealthRegistry()
    registry.add(HealthyChecker("db"))
    registry.add(UnhealthyChecker("redis"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    components = {c["name"]: c for c in report["components"]}
    assert components["db"]["status"] == HealthStatus.HEALTHY
    assert components["redis"]["status"] == HealthStatus.UNHEALTHY


async def test_all_healthy() -> None:
    """Test registry with multiple healthy checkers."""
    registry = HealthRegistry()
    registry.add(HealthyChecker("db"))
    registry.add(HealthyChecker("redis"))
    registry.add(HealthyChecker("kafka"))

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert len(report["components"]) == len(["db", "kafka", "redis"])


async def test_checker_timeout() -> None:
    """Test that slow checkers are reported as UNHEALTHY."""
    registry = HealthRegistry(timeout=0.1)
    registry.add(SlowChecker("slow_db"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "slow_db"
    assert report["components"][0]["status"] == HealthStatus.UNHEALTHY
    assert "Timed out" in (report["components"][0]["detail"] or "")


async def test_duplicate_name_raises() -> None:
    """Test that registering a checker with a duplicate name raises."""
    registry = HealthRegistry()
    registry.add(HealthyChecker("db"))

    with pytest.raises(ValueError, match="already registered"):
        registry.add(HealthyChecker("db"))


async def test_components_sorted_by_name() -> None:
    """Test that components in the report are sorted alphabetically."""
    registry = HealthRegistry()
    registry.add(HealthyChecker("zeta"))
    registry.add(HealthyChecker("alpha"))
    registry.add(HealthyChecker("middle"))

    report = await registry.check()

    names = [c["name"] for c in report["components"]]
    assert names == ["alpha", "middle", "zeta"]


def test_health_check_timeout_error() -> None:
    """Test HealthCheckTimeoutError message formatting."""
    timeout = 5.0
    error = HealthCheckTimeoutError(name="db", timeout=timeout)

    assert error.name == "db"
    assert error.timeout == timeout
    assert "db" in str(error)
    assert "5.0" in str(error)


async def test_concurrent_execution() -> None:
    """Test that checkers run concurrently (not sequentially)."""
    checker_count = 3
    registry = HealthRegistry(timeout=2.0)
    for i in range(checker_count):
        registry.add(SlowChecker(f"checker_{i}", delay=0.1))

    start = time.monotonic()
    report = await registry.check()
    elapsed = time.monotonic() - start

    assert report["status"] == OverallStatus.HEALTHY
    # If concurrent, should finish in ~0.1s, not ~0.3s
    max_concurrent_time = 0.25
    assert elapsed < max_concurrent_time
