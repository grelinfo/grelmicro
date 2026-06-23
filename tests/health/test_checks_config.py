"""Tests for the three-paths HealthChecks construction."""

import pytest

from grelmicro.errors import SettingsValidationError
from grelmicro.health import HealthSettingsValidationError
from grelmicro.health._checks import HealthChecks, HealthChecksConfig

TIMEOUT_KWARG = 2.5
CACHE_TTL_KWARG = 0.5
TIMEOUT_ENV = 7.5
CACHE_TTL_ENV = 3.0
DEFAULT_TIMEOUT = 5.0
DEFAULT_CACHE_TTL = 1.0


def test_programmatic_path_uses_kwargs() -> None:
    """Plain kwargs build a config, falling back to defaults."""
    registry = HealthChecks(timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG)
    assert registry._config.timeout == TIMEOUT_KWARG
    assert registry._config.cache_ttl == CACHE_TTL_KWARG


def test_declarative_path_uses_from_config() -> None:
    """`HealthChecks.from_config()` constructs from a pre-built config."""
    cfg = HealthChecksConfig(timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG)
    registry = HealthChecks.from_config(cfg)
    assert registry._config is cfg


def test_from_config_bypasses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HealthChecks.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    cfg = HealthChecksConfig(timeout=TIMEOUT_KWARG, cache_ttl=CACHE_TTL_KWARG)
    registry = HealthChecks.from_config(cfg)
    assert registry._config.timeout == TIMEOUT_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_HEALTH_*`` populate unset fields."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    monkeypatch.setenv("GREL_HEALTH_CACHE_TTL", str(CACHE_TTL_ENV))
    registry = HealthChecks()
    assert registry._config.timeout == TIMEOUT_ENV
    assert registry._config.cache_ttl == CACHE_TTL_ENV


def test_named_instance_reads_name_segmented_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named instance reads ``GREL_HEALTH_{NAME}_*``, not the bare prefix."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_KWARG))
    monkeypatch.setenv("GREL_HEALTH_API_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthChecks(name="api")
    assert registry._config.timeout == TIMEOUT_ENV


def test_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthChecks(timeout=TIMEOUT_KWARG)
    assert registry._config.timeout == TIMEOUT_KWARG


def test_env_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_HEALTH_``."""
    monkeypatch.setenv("MYAPP_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthChecks(env_prefix="MYAPP_HEALTH_")
    assert registry._config.timeout == TIMEOUT_ENV


def test_env_load_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_load=False`` skips env reads entirely."""
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", str(TIMEOUT_ENV))
    registry = HealthChecks(env_load=False)
    assert registry._config.timeout == DEFAULT_TIMEOUT


def test_zero_config_uses_health_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, HealthChecksConfig defaults take over."""
    monkeypatch.delenv("GREL_HEALTH_TIMEOUT", raising=False)
    monkeypatch.delenv("GREL_HEALTH_CACHE_TTL", raising=False)
    registry = HealthChecks()
    assert registry._config.timeout == DEFAULT_TIMEOUT
    assert registry._config.cache_ttl == DEFAULT_CACHE_TTL


def test_from_config_keeps_default_name() -> None:
    """`from_config(...)` defaults to name='default'."""
    cfg = HealthChecksConfig()
    registry = HealthChecks.from_config(cfg)
    assert registry.name == "default"


def test_invalid_config_raises_settings_error() -> None:
    """Invalid kwargs raise a catchable `HealthSettingsValidationError`."""
    with pytest.raises(HealthSettingsValidationError) as exc_info:
        HealthChecks(timeout=-5)
    assert isinstance(exc_info.value, SettingsValidationError)
    assert "timeout" in str(exc_info.value)
    assert "-5" not in str(exc_info.value)


def test_settings_error_is_settings_validation_error() -> None:
    """`HealthSettingsValidationError` is a `SettingsValidationError`."""
    assert issubclass(HealthSettingsValidationError, SettingsValidationError)
