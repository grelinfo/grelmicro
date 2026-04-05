"""Tests for Health Check Registry."""

import time
from collections.abc import Generator

import pytest

from grelmicro.health._models import HealthStatus, OverallStatus
from grelmicro.health._registry import HealthRegistry
from grelmicro.health._state import (
    HealthRegistryNotLoadedError,
    get_health_registry,
    reset_health_registry,
    set_health_registry,
)
from grelmicro.health.errors import HealthCheckTimeoutError

from .conftest import (
    HealthyChecker,
    HealthyCheckerWithDetails,
    SlowChecker,
    UnhealthyChecker,
)

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None]:
    """Reset global health registry before and after each test."""
    reset_health_registry()
    yield
    reset_health_registry()


async def test_empty_registry_is_healthy() -> None:
    """Test that a registry with no checkers returns HEALTHY."""
    registry = HealthRegistry(auto_register=False)

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert report["components"] == []


async def test_single_healthy_checker() -> None:
    """Test registry with one healthy checker."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "db"
    assert report["components"][0]["status"] == HealthStatus.HEALTHY
    assert report["components"][0]["critical"] is True
    assert report["components"][0]["error"] is None
    assert report["components"][0]["details"] is None


async def test_single_unhealthy_checker() -> None:
    """Test registry with one failing checker."""
    registry = HealthRegistry(auto_register=False)
    registry.add(UnhealthyChecker("redis"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "redis"
    assert report["components"][0]["status"] == HealthStatus.UNHEALTHY
    assert report["components"][0]["error"] == "Connection refused"
    assert report["components"][0]["details"] is None


async def test_checker_with_details() -> None:
    """Test that checker details are captured in the report."""
    registry = HealthRegistry(auto_register=False)
    registry.add(
        HealthyCheckerWithDetails(
            "redis", {"latency_ms": 1.5, "version": "7.2"}
        )
    )

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert report["components"][0]["status"] == HealthStatus.HEALTHY
    assert report["components"][0]["details"] == {
        "latency_ms": 1.5,
        "version": "7.2",
    }


async def test_mixed_healthy_and_unhealthy() -> None:
    """Test registry with both healthy and unhealthy checkers."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))
    registry.add(UnhealthyChecker("redis"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    components = {c["name"]: c for c in report["components"]}
    assert components["db"]["status"] == HealthStatus.HEALTHY
    assert components["redis"]["status"] == HealthStatus.UNHEALTHY


async def test_all_healthy() -> None:
    """Test registry with multiple healthy checkers."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))
    registry.add(HealthyChecker("redis"))
    registry.add(HealthyChecker("kafka"))

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert [c["name"] for c in report["components"]] == ["db", "kafka", "redis"]


async def test_checker_timeout() -> None:
    """Test that slow checkers are reported as UNHEALTHY."""
    registry = HealthRegistry(timeout=0.1, auto_register=False)
    registry.add(SlowChecker("slow_db"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED
    assert len(report["components"]) == 1
    assert report["components"][0]["name"] == "slow_db"
    assert report["components"][0]["status"] == HealthStatus.UNHEALTHY
    assert (
        report["components"][0]["error"]
        == "Health check 'slow_db' timed out after 0.1s"
    )


async def test_duplicate_name_raises() -> None:
    """Test that registering a checker with a duplicate name raises."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))

    with pytest.raises(ValueError, match="already registered"):
        registry.add(HealthyChecker("db"))


async def test_components_sorted_by_name() -> None:
    """Test that components in the report are sorted alphabetically."""
    registry = HealthRegistry(auto_register=False)
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
    checker_delay = 0.1
    registry = HealthRegistry(timeout=2.0, auto_register=False)
    for i in range(checker_count):
        registry.add(SlowChecker(f"checker_{i}", delay=checker_delay))

    start = time.monotonic()
    report = await registry.check()
    elapsed = time.monotonic() - start

    assert report["status"] == OverallStatus.HEALTHY
    # If concurrent, should finish in ~0.1s; sequential would be ~0.3s
    sequential_bound = checker_count * checker_delay
    assert elapsed < sequential_bound


def test_auto_register() -> None:
    """Test that HealthRegistry auto-registers as the global singleton."""
    registry = HealthRegistry()

    assert get_health_registry() is registry


def test_auto_register_false() -> None:
    """Test that auto_register=False skips global registration."""
    HealthRegistry(auto_register=False)

    with pytest.raises(HealthRegistryNotLoadedError):
        get_health_registry()


def test_get_health_registry_raises_when_not_loaded() -> None:
    """Test that get_health_registry raises when no registry exists."""
    with pytest.raises(HealthRegistryNotLoadedError):
        get_health_registry()


def test_overwrite_warns() -> None:
    """Test that overwriting an existing registry emits a warning."""
    HealthRegistry()

    with pytest.warns(UserWarning, match="Overwriting"):
        HealthRegistry()


def test_set_and_reset() -> None:
    """Test set_health_registry and reset_health_registry."""
    registry = HealthRegistry(auto_register=False)
    set_health_registry(registry)

    assert get_health_registry() is registry

    reset_health_registry()

    with pytest.raises(HealthRegistryNotLoadedError):
        get_health_registry()


async def test_non_critical_failure_does_not_degrade() -> None:
    """Test that a non-critical checker failure keeps overall status HEALTHY."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))
    registry.add(UnhealthyChecker("external-api"), critical=False)

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    components = {c["name"]: c for c in report["components"]}
    assert components["db"]["status"] == HealthStatus.HEALTHY
    assert components["db"]["critical"] is True
    assert components["external-api"]["status"] == HealthStatus.UNHEALTHY
    assert components["external-api"]["critical"] is False


async def test_critical_failure_degrades() -> None:
    """Test that a critical checker failure causes DEGRADED status."""
    registry = HealthRegistry(auto_register=False)
    registry.add(UnhealthyChecker("db"), critical=True)
    registry.add(HealthyChecker("external-api"))

    report = await registry.check()

    assert report["status"] == OverallStatus.DEGRADED


async def test_only_non_critical_checkers_all_failing() -> None:
    """Test that all non-critical failures still report HEALTHY overall."""
    registry = HealthRegistry(auto_register=False)
    registry.add(UnhealthyChecker("api-a"), critical=False)
    registry.add(UnhealthyChecker("api-b"), critical=False)

    report = await registry.check()

    assert report["status"] == OverallStatus.HEALTHY
    assert all(
        c["status"] == HealthStatus.UNHEALTHY for c in report["components"]
    )


async def test_critical_defaults_to_true() -> None:
    """Test that checkers are critical by default."""
    registry = HealthRegistry(auto_register=False)
    registry.add(HealthyChecker("db"))

    report = await registry.check()

    assert report["components"][0]["critical"] is True
