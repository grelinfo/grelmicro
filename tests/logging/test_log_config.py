"""Tests for the three-paths `log.configure` construction contract."""

import pytest

from grelmicro.errors import GrelmicroConfigWarning, SettingsValidationError
from grelmicro.log import (
    LogConfig,
    configure,
    configure_with,
)
from grelmicro.log.config import LogBackendType, LogFormatType, LogLevelType
from tests.logging.conftest import parse_json_log


def test_configure_returns_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure()` returns the `LogConfig` it applied."""
    monkeypatch.delenv("GREL_LOG_LEVEL", raising=False)
    cfg = configure(level=LogLevelType.DEBUG)
    assert isinstance(cfg, LogConfig)
    assert cfg.level == LogLevelType.DEBUG


def test_configure_kwargs_override_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """Caller kwargs win over `GREL_LOG_*` env vars."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "WARNING")
    cfg = configure(level=LogLevelType.DEBUG)
    assert cfg.level == LogLevelType.DEBUG


def test_configure_reads_env_when_kwargs_unset(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`GREL_LOG_*` env vars populate fields when kwargs are unset."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("GREL_LOG_BACKEND", "stdlib")
    cfg = configure()
    assert cfg.level == LogLevelType.ERROR
    assert cfg.backend == LogBackendType.STDLIB


def test_configure_env_load_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`env_load=False` skips env reading entirely."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    cfg = configure(env_load=False)
    assert cfg.level == LogLevelType.INFO  # default


def test_configure_reports_ignored_env_in_the_log_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reset_backend: None,  # noqa: ARG001
) -> None:
    """A variable set without the opt-in is named in the application's format."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_LOG_LEVEL", "DEBUG")

    with pytest.warns(GrelmicroConfigWarning):
        configure(format=LogFormatType.JSON)

    record = parse_json_log(capsys.readouterr().out)
    assert record["level"] == "WARNING"
    assert record["variable"] == "GREL_LOG_LEVEL"
    assert "GREL_ENV_LOAD=1" in record["msg"]


def test_configure_invalid_level_raises_settings_error(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """Invalid env values raise a catchable `SettingsValidationError`."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "BOGUS")
    with pytest.raises(SettingsValidationError) as exc_info:
        configure()
    assert isinstance(exc_info.value, SettingsValidationError)


def test_configure_with_returns_passed_config(
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure_with(cfg)` returns the same `LogConfig` for symmetry."""
    cfg = LogConfig(level=LogLevelType.WARNING)
    returned = configure_with(cfg)
    assert returned is cfg


def test_configure_with_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """`configure_with(cfg)` ignores env vars and uses the passed config as-is."""
    monkeypatch.setenv("GREL_LOG_LEVEL", "ERROR")
    cfg = LogConfig(level=LogLevelType.WARNING)
    returned = configure_with(cfg)
    assert returned.level == LogLevelType.WARNING
