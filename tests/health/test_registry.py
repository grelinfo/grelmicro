"""Tests for Health Check Registry."""

import logging
import time
from collections.abc import Generator

import anyio
import pytest
from pydantic import ValidationError

from grelmicro._backends import BackendNotLoadedError
from grelmicro.health._backends import get_health_registry, health_registry
from grelmicro.health._models import HealthStatus
from grelmicro.health._registry import HealthRegistry
from grelmicro.health._types import HealthDetails

from .conftest import (
    Counting,
    SlowCounting,
    healthy,
    healthy_with_details,
    slow,
    unhealthy,
    unhealthy_with_health_error,
)

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None]:
    """Reset global health registry before and after each test."""
    health_registry.reset()
    yield
    health_registry.reset()


async def test_empty_registry_is_ok() -> None:
    """An empty registry reports ok."""
    registry = HealthRegistry(auto_register=False)

    report = await registry.run()

    assert report["status"] == HealthStatus.OK
    assert report["checks"] == {}


async def test_add_and_run_single_healthy() -> None:
    """registry.add() + run() reports ok for a healthy check."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", healthy())

    report = await registry.run()

    assert report["status"] == HealthStatus.OK
    assert list(report["checks"]) == ["db"]
    db = report["checks"]["db"]
    assert db["status"] == HealthStatus.OK
    assert db["critical"] is True
    assert db["error"] is None
    assert db["details"] is None


async def test_decorator_registers_check() -> None:
    """@registry.check(name) registers the decorated function."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)

    @registry.check("database")
    async def _check_db() -> HealthDetails | None:
        return None

    report = await registry.run()

    assert list(report["checks"]) == ["database"]
    assert report["status"] == HealthStatus.OK


async def test_sync_check_runs_in_thread() -> None:
    """A sync check function is executed via ``anyio.to_thread.run_sync``."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)

    def sync_check() -> HealthDetails | None:
        return {"ran": "sync"}

    registry.add("db", sync_check)

    report = await registry.run()

    assert report["status"] == HealthStatus.OK
    assert report["checks"]["db"]["details"] == {"ran": "sync"}


async def test_decorator_returns_function_unchanged() -> None:
    """The decorator returns the wrapped function as-is."""
    registry = HealthRegistry(auto_register=False)

    async def _fn() -> HealthDetails | None:
        return {"ok": True}

    wrapped = registry.check("x")(_fn)

    assert wrapped is _fn


async def test_decorator_with_options() -> None:
    """Decorator accepts critical and timeout kwargs."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0, timeout=5.0)

    @registry.check("analytics", critical=False, timeout=0.1)
    async def _check() -> HealthDetails | None:
        await anyio.sleep(1.0)
        return None

    started = time.monotonic()
    report = await registry.run()
    elapsed = time.monotonic() - started

    assert report["status"] == HealthStatus.OK  # non-critical fail ignored
    assert report["checks"]["analytics"]["status"] == HealthStatus.ERROR
    assert report["checks"]["analytics"]["critical"] is False
    assert elapsed < 1.0  # per-check timeout honored


async def test_critical_failure_produces_error() -> None:
    """Critical failure produces aggregate error."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("redis", unhealthy())

    report = await registry.run()

    assert report["status"] == HealthStatus.ERROR
    redis = report["checks"]["redis"]
    assert redis["status"] == HealthStatus.ERROR
    assert redis["error"] == "Health check failed"
    assert redis["details"] is None


async def test_non_critical_failure_keeps_aggregate_ok() -> None:
    """Non-critical failures do not flip the aggregate."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", healthy())
    registry.add("external-api", unhealthy(), critical=False)

    report = await registry.run()

    assert report["status"] == HealthStatus.OK
    assert report["checks"]["db"]["status"] == HealthStatus.OK
    assert report["checks"]["external-api"]["status"] == HealthStatus.ERROR
    assert report["checks"]["external-api"]["critical"] is False


async def test_critical_failure_trumps_non_critical() -> None:
    """Any critical failure produces aggregate error."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", unhealthy(), critical=True)
    registry.add("analytics", unhealthy(), critical=False)

    report = await registry.run()

    assert report["status"] == HealthStatus.ERROR


async def test_all_non_critical_fail_aggregate_ok() -> None:
    """All non-critical failures keep the aggregate ok."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("a", unhealthy(), critical=False)
    registry.add("b", unhealthy(), critical=False)

    report = await registry.run()

    assert report["status"] == HealthStatus.OK


async def test_check_with_details() -> None:
    """Checker details are captured in the result."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    report = await registry.run()

    assert report["checks"]["redis"]["details"] == {"latency_ms": 1.5}


async def test_checks_sorted_by_name() -> None:
    """Checks are returned in alphabetical order."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("zeta", healthy())
    registry.add("alpha", healthy())
    registry.add("mu", healthy())

    report = await registry.run()

    assert list(report["checks"]) == ["alpha", "mu", "zeta"]


async def test_critical_only_filter() -> None:
    """critical_only=True skips non-critical checks entirely."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", healthy())
    registry.add("analytics", unhealthy(), critical=False)

    report = await registry.run(critical_only=True)

    assert list(report["checks"]) == ["db"]
    assert report["status"] == HealthStatus.OK


async def test_exclude_filter() -> None:
    """Exclude skips the named checks."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", healthy())
    registry.add("redis", unhealthy())

    report = await registry.run(exclude=["redis"])

    assert list(report["checks"]) == ["db"]
    assert report["status"] == HealthStatus.OK


async def test_global_timeout_applies() -> None:
    """Global timeout applies when no per-check timeout is set."""
    registry = HealthRegistry(timeout=0.1, auto_register=False, cache_ttl=0)
    registry.add("slow", slow(delay=1.0))

    report = await registry.run()

    assert report["status"] == HealthStatus.ERROR
    slow_result = report["checks"]["slow"]
    assert slow_result["status"] == HealthStatus.ERROR
    error = slow_result["error"]
    assert error is not None
    assert "timed out" in error
    assert "0.1" in error


async def test_per_check_timeout_override_via_add() -> None:
    """Per-check timeout overrides the registry default via add()."""
    registry = HealthRegistry(timeout=5.0, auto_register=False, cache_ttl=0)
    registry.add("slow", slow(delay=1.0), timeout=0.1, critical=False)

    started = time.monotonic()
    report = await registry.run()
    elapsed = time.monotonic() - started

    assert report["status"] == HealthStatus.OK
    assert report["checks"]["slow"]["status"] == HealthStatus.ERROR
    assert elapsed < 1.0


async def test_concurrent_execution() -> None:
    """Checks run in parallel, not sequentially."""
    count = 3
    delay = 0.1
    registry = HealthRegistry(timeout=2.0, auto_register=False, cache_ttl=0)
    for i in range(count):
        registry.add(f"c{i}", slow(delay=delay))

    started = time.monotonic()
    report = await registry.run()
    elapsed = time.monotonic() - started

    assert report["status"] == HealthStatus.OK
    assert elapsed < count * delay


async def test_cache_hit_returns_same_result() -> None:
    """Within TTL, repeated calls return the cached result."""
    registry = HealthRegistry(auto_register=False, cache_ttl=10.0)
    check = Counting()
    registry.add("db", check)

    await registry.run()
    await registry.run()
    await registry.run()

    assert check.calls == 1


async def test_cache_expires() -> None:
    """After TTL expires, the check runs again."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0.05)
    check = Counting()
    registry.add("db", check)

    await registry.run()
    await anyio.sleep(0.1)
    await registry.run()

    expected_calls = 2
    assert check.calls == expected_calls


async def test_cache_disabled_with_zero_ttl() -> None:
    """cache_ttl=0 disables the cache entirely."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    check = Counting()
    registry.add("db", check)

    await registry.run()
    await registry.run()

    expected_calls = 2
    assert check.calls == expected_calls


async def test_single_flight_coalesces_concurrent_calls() -> None:
    """Concurrent cache-fill requests share a single execution."""
    registry = HealthRegistry(auto_register=False, cache_ttl=1.0)
    check = SlowCounting(delay=0.1)
    registry.add("db", check)

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(registry.run)

    assert check.calls == 1


async def test_shared_cache_between_readyz_and_healthz_paths() -> None:
    """critical_only=True and full run share per-check cache."""
    registry = HealthRegistry(auto_register=False, cache_ttl=10.0)
    critical = Counting()
    non_critical = Counting()
    registry.add("db", critical)
    registry.add("analytics", non_critical, critical=False)

    await registry.run(critical_only=True)
    await registry.run()

    assert critical.calls == 1
    assert non_critical.calls == 1


async def test_duplicate_name_raises_for_add() -> None:
    """Registering two checks with the same name via add() raises."""
    registry = HealthRegistry(auto_register=False)
    registry.add("db", healthy())

    with pytest.raises(ValueError, match="already registered"):
        registry.add("db", healthy())


async def test_duplicate_name_raises_for_decorator() -> None:
    """The decorator form also rejects duplicates."""
    registry = HealthRegistry(auto_register=False)

    @registry.check("db")
    async def _first() -> HealthDetails | None:
        return None

    with pytest.raises(ValueError, match="already registered"):

        @registry.check("db")
        async def _second() -> HealthDetails | None:
            return None


async def test_health_error_exposes_message() -> None:
    """HealthError subclasses expose their message in the error field."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", unhealthy_with_health_error())

    report = await registry.run()

    assert (
        report["checks"]["db"]["error"] == "Database connection pool exhausted"
    )


async def test_generic_exception_hides_message() -> None:
    """Non-HealthError exceptions get a generic message."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("redis", unhealthy())

    report = await registry.run()

    assert report["checks"]["redis"]["error"] == "Health check failed"


async def test_health_error_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HealthError failures log at WARNING with exc_info."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("db", unhealthy_with_health_error())

    with caplog.at_level(logging.WARNING, logger="grelmicro.health"):
        await registry.run()

    records = [r for r in caplog.records if r.name == "grelmicro.health"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert "db" in record.getMessage()
    assert record.exc_info is not None


async def test_generic_exception_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected exceptions log at ERROR with a traceback."""
    registry = HealthRegistry(auto_register=False, cache_ttl=0)
    registry.add("redis", unhealthy())

    with caplog.at_level(logging.WARNING, logger="grelmicro.health"):
        await registry.run()

    records = [r for r in caplog.records if r.name == "grelmicro.health"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


async def test_timeout_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Timed-out checks log at WARNING with the per-check timeout."""
    registry = HealthRegistry(timeout=0.05, auto_register=False, cache_ttl=0)
    registry.add("slow", slow(delay=1.0))

    with caplog.at_level(logging.WARNING, logger="grelmicro.health"):
        await registry.run()

    records = [r for r in caplog.records if r.name == "grelmicro.health"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.args == ("slow", 0.05)
    assert record.exc_info is None


def test_timeout_zero_raises() -> None:
    """timeout=0 is rejected."""
    with pytest.raises(ValidationError, match="greater than 0"):
        HealthRegistry(timeout=0, auto_register=False)


def test_timeout_negative_raises() -> None:
    """Negative timeout is rejected."""
    with pytest.raises(ValidationError, match="greater than 0"):
        HealthRegistry(timeout=-1.0, auto_register=False)


def test_cache_ttl_negative_raises() -> None:
    """Negative cache_ttl is rejected."""
    with pytest.raises(ValidationError):
        HealthRegistry(cache_ttl=-1.0, auto_register=False)


def test_auto_register() -> None:
    """HealthRegistry auto-registers as the global singleton."""
    registry = HealthRegistry()

    assert get_health_registry() is registry


def test_auto_register_false() -> None:
    """auto_register=False skips global registration."""
    HealthRegistry(auto_register=False)

    with pytest.raises(BackendNotLoadedError):
        get_health_registry()


def test_get_health_registry_raises_when_not_loaded() -> None:
    """get_health_registry raises before a registry is created."""
    with pytest.raises(BackendNotLoadedError):
        get_health_registry()


def test_overwrite_warns() -> None:
    """Overwriting an existing registry emits a warning."""
    HealthRegistry()

    with pytest.warns(UserWarning, match="Overwriting"):
        HealthRegistry()


def test_set_health_registry() -> None:
    """health_registry.set installs the singleton."""
    registry = HealthRegistry(auto_register=False)

    health_registry.set(registry)

    assert get_health_registry() is registry


def test_reset_health_registry() -> None:
    """health_registry.reset removes the singleton."""
    registry = HealthRegistry(auto_register=False)
    health_registry.set(registry)

    health_registry.reset()

    with pytest.raises(BackendNotLoadedError):
        get_health_registry()
