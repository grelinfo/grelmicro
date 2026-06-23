"""Tests for the `Log` component (Grelmicro app integration)."""

from __future__ import annotations

import logging

import pytest

from grelmicro import Component, ComponentAlreadyRegisteredError, Grelmicro
from grelmicro.log import Log, LogConfig
from grelmicro.log.config import LogLevelType


def test_log_satisfies_component_protocol() -> None:
    """`Log` is a runtime-checkable `Component`."""
    assert isinstance(Log(), Component)


def test_log_default_kind_and_name() -> None:
    """Default kind is `log` and default name is `default`."""
    log = Log()
    assert log.kind == "log"
    assert log.name == "default"


def test_log_is_singleton() -> None:
    """`Log` configures the global root logger, so a second one is refused."""
    with pytest.raises(ComponentAlreadyRegisteredError, match="singleton"):
        Grelmicro(uses=[Log(), Log(name="audit")])


def test_log_name_is_read_only() -> None:
    """`Log.name` is a read-only property."""
    log = Log()
    with pytest.raises(AttributeError):
        log.name = "other"  # ty: ignore[invalid-assignment]


def test_log_config_unavailable_before_enter() -> None:
    """`Log.config` raises before the component has been entered."""
    log = Log()
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = log.config


async def test_log_resolves_config_on_enter(
    reset_stdlib: None,  # noqa: ARG001
) -> None:
    """Entering the app resolves the config and configures logging."""
    micro = Grelmicro(uses=[Log(level=LogLevelType.DEBUG)])
    async with micro:
        assert micro.log.config.level == LogLevelType.DEBUG
        assert logging.getLogger().level == logging.DEBUG


async def test_log_accepts_prebuilt_config(reset_stdlib: None) -> None:  # noqa: ARG001
    """`Log(config=...)` uses the pre-built `LogConfig` as-is."""
    config = LogConfig(level=LogLevelType.WARNING)
    micro = Grelmicro(uses=[Log(config=config)])
    async with micro:
        assert micro.log.config is config


def test_log_from_config_matches_config_kwarg() -> None:
    """`Log.from_config(cfg)` matches `Log(config=cfg)`."""
    config = LogConfig(level=LogLevelType.WARNING)
    log = Log.from_config(config)
    assert log._explicit_config is config
    assert log.name == "default"


def test_log_from_config_keeps_name() -> None:
    """`Log.from_config(..., name=...)` keeps the registration name."""
    config = LogConfig()
    log = Log.from_config(config, name="audit")
    assert log.name == "audit"


async def test_log_restores_root_handlers_on_exit(
    reset_stdlib: None,  # noqa: ARG001
) -> None:
    """Exiting restores the stdlib root logger handlers and level."""
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    before = list(root.handlers)
    root.setLevel(logging.WARNING)
    micro = Grelmicro(uses=[Log(level=LogLevelType.DEBUG)])
    async with micro:
        assert sentinel not in root.handlers
    assert root.handlers == before
    assert root.level == logging.WARNING


async def test_log_use_via_micro_attribute(reset_stdlib: None) -> None:  # noqa: ARG001
    """`micro.log` resolves to the registered `Log` component."""
    micro = Grelmicro(uses=[Log()])
    async with micro:
        assert isinstance(micro.log, Log)
