"""Tests for the three-paths `log.configure` construction contract."""

import pytest

from grelmicro.errors import SettingsValidationError
from grelmicro.log import (
    LoggingConfig,
    LoggingSettingsValidationError,
    configure,
    configure_with,
)
from grelmicro.log.config import LoggingBackendType, LoggingLevelType


def test_configure_returns_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure()` returns the `LoggingConfig` it applied."""
    monkeypatch.delenv("GREL_LOG_LEVEL", raising=False)
    cfg = configure(level=LoggingLevelType.DEBUG)
    assert isinstance(cfg, LoggingConfig)
    assert cfg.level == LoggingLevelType.DEBUG


def test_configure_kwargs_override_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """Caller kwargs win over `GREL_LOG_*` env vars."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "WARNING")
    cfg = configure(level=LoggingLevelType.DEBUG)
    assert cfg.level == LoggingLevelType.DEBUG


def test_configure_reads_env_when_kwargs_unset(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`GREL_LOG_*` env vars populate fields when kwargs are unset."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("GREL_LOG_BACKEND", "stdlib")
    cfg = configure()
    assert cfg.level == LoggingLevelType.ERROR
    assert cfg.backend == LoggingBackendType.STDLIB


def test_configure_env_load_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`env_load=False` skips env reading entirely."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    cfg = configure(env_load=False)
    assert cfg.level == LoggingLevelType.INFO  # default


def test_configure_invalid_level_raises_settings_error(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """Invalid env values raise a catchable `LoggingSettingsValidationError`."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "BOGUS")
    with pytest.raises(LoggingSettingsValidationError) as exc_info:
        configure()
    assert isinstance(exc_info.value, SettingsValidationError)


def test_logging_settings_error_is_settings_validation_error() -> None:
    """`LoggingSettingsValidationError` is a `SettingsValidationError`."""
    assert issubclass(LoggingSettingsValidationError, SettingsValidationError)


def test_configure_with_returns_passed_config(
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure_with(cfg)` returns the same `LoggingConfig` for symmetry."""
    cfg = LoggingConfig(level=LoggingLevelType.WARNING)
    returned = configure_with(cfg)
    assert returned is cfg


def test_configure_with_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure_with(cfg)` ignores env vars and uses the passed config as-is."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    cfg = LoggingConfig(level=LoggingLevelType.WARNING)
    returned = configure_with(cfg)
    assert returned.level == LoggingLevelType.WARNING
