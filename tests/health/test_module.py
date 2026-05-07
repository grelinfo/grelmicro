"""Tests for the `Health` module (Grelmicro app integration)."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro, Module
from grelmicro.health import (
    Health,
    HealthRegistry,
    HealthStatus,
)


def test_health_satisfies_module_protocol() -> None:
    """`Health` is a runtime-checkable `Module`."""
    assert isinstance(Health(), Module)


def test_health_default_kind_and_name() -> None:
    """Default kind is `health` and default name is `default`."""
    health = Health()
    assert health.kind == "health"
    assert health.name == "default"


def test_health_constructor_creates_default_registry() -> None:
    """When no registry is passed, a fresh `HealthRegistry` is constructed."""
    health = Health()
    assert isinstance(health.registry, HealthRegistry)


def test_health_constructor_accepts_external_registry() -> None:
    """An existing `HealthRegistry` can be wrapped."""
    registry = HealthRegistry()
    health = Health(registry=registry)
    assert health.registry is registry


def test_health_named_registration() -> None:
    """A named `Health` module coexists with the default one."""
    micro = Grelmicro(modules=[Health(), Health(name="readiness")])
    assert micro.get("health", "default").name == "default"
    assert micro.get("health", "readiness").name == "readiness"


async def test_health_check_decorator_via_module() -> None:
    """`@health.check(name)` registers a check on the wrapped registry."""
    health = Health()

    @health.check("ok")
    async def ok_check() -> None:
        return None

    async with health:
        report = await health.run()
        assert report["status"] == HealthStatus.OK
        assert "ok" in report["checks"]


async def test_health_check_via_micro_attribute() -> None:
    """`@micro.health.check(name)` is the conventional access path."""
    micro = Grelmicro(modules=[Health()])

    @micro.health.check("ping")
    async def ping() -> None:
        return None

    async with micro:
        report = await micro.health.run()
        assert report["status"] == HealthStatus.OK


async def test_health_run_aggregates_failures() -> None:
    """A failing check flips the aggregate status away from OK."""
    micro = Grelmicro(modules=[Health()])
    msg = "down"

    @micro.health.check("db")
    async def db_check() -> None:
        raise RuntimeError(msg)

    async with micro:
        report = await micro.health.run()
        assert report["status"] != HealthStatus.OK
        assert "db" in report["checks"]


async def test_micro_health_prefers_default_over_named() -> None:
    """`micro.health` resolves to the default-named module."""
    primary = HealthRegistry()
    micro = Grelmicro(
        modules=[
            Health(registry=primary),
            Health(name="readiness"),
        ]
    )
    assert micro.health.registry is primary


async def test_micro_health_raises_when_ambiguous() -> None:
    """`micro.health` raises when multiple non-default modules exist."""
    micro = Grelmicro(
        modules=[
            Health(name="liveness"),
            Health(name="readiness"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'health' modules"):
        _ = micro.health
