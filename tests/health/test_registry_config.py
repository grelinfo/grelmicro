"""Tests for the three-paths HealthRegistry construction."""

import pytest

from grelmicro.health._backends import health_registry
from grelmicro.health._registry import HealthRegistry, HealthRegistryConfig

TIMEOUT_KWARG = 2.5
CACHE_TTL_KWARG = 0.5
TIMEOUT_ENV = 7.5
CACHE_TTL_ENV = 3.0
DEFAULT_TIMEOUT = 5.0
DEFAULT_CACHE_TTL = 1.0


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the global health registry between tests."""
    health_registry.reset()


def test_programmatic_path_uses_kwargs() -> None:
    """Plain kwargs build a config, falling back to defaults."""
    registry = HealthRegistry(
        timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG, auto_register=False
    )
    assert registry._config.timeout == TIMEOUT_KWARG
    assert registry._config.cache_ttl == CACHE_TTL_KWARG


def test_declarative_path_uses_from_config() -> None:
    """`HealthRegistry.from_config()` constructs from a pre-built config."""
    cfg = HealthRegistryConfig(timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG)
    registry = HealthRegistry.from_config(cfg, auto_register=False)
    assert registry._config is cfg


def test_from_config_bypasses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HealthRegistry.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    cfg = HealthRegistryConfig(timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG)
    registry = HealthRegistry.from_config(cfg, auto_register=False)
    assert registry._config.timeout == TIMEOUT_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_HEALTH_*`` populate unset fields."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    monkeypatch.setenv("GREL_HEALTH_CACHE_TTL", str(CACHE_TTL_ENV))
    registry = HealthRegistry(auto_register=False)
    assert registry._config.timeout == TIMEOUT_ENV
    assert registry._config.cache_ttl == CACHE_TTL_ENV


def test_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthRegistry(timeout=TIMEOUT_KWARG, auto_register=False)
    assert registry._config.timeout == TIMEOUT_KWARG


def test_env_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_HEALTH_``."""
    monkeypatch.setenv("MYAPP_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthRegistry(env_prefix="MYAPP_HEALTH_", auto_register=False)
    assert registry._config.timeout == TIMEOUT_ENV


def test_read_env_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthRegistry(read_env=False, auto_register=False)
    assert registry._config.timeout == DEFAULT_TIMEOUT


def test_zero_config_uses_health_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, HealthRegistryConfig defaults take over."""
    monkeypatch.delenv("GREL_HEALTH_TIMEOUT", raising=False)
    monkeypatch.delenv("GREL_HEALTH_CACHE_TTL", raising=False)
    registry = HealthRegistry(auto_register=False)
    assert registry._config.timeout == DEFAULT_TIMEOUT
    assert registry._config.cache_ttl == DEFAULT_CACHE_TTL


def test_from_config_auto_register() -> None:
    """`from_config(..., auto_register=True)` registers the instance globally."""
    cfg = HealthRegistryConfig()
    registry = HealthRegistry.from_config(cfg, auto_register=True)
    assert health_registry.get() is registry
