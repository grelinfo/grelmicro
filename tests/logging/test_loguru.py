"""Unit Tests for Loguru Backend.

These tests verify loguru-specific internal implementation details
that are not shared with other backends.
"""

from __future__ import annotations

from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest_mock
    from loguru import Record

from grelmicro.log._loguru import (
    JSON_FORMAT,
    _json_formatter,
    _json_patcher,
    _LoguruPatcher,
    configure,
)
from tests.logging.conftest import parse_json_log

_USER_ID = 123
_USER_ID_2 = 456
_USER_ID_3 = 789


@pytest.fixture(autouse=True)
def cleanup_handlers() -> Generator[None, None, None]:
    """Cleanup logging handlers."""
    logger.configure(handlers=[])
    yield
    logger.remove()


def generate_logs() -> int:
    """Generate logs."""
    logger.debug("Hello, World!")
    logger.info("Hello, World!")
    logger.warning("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")
    try:
        1 / 0  # noqa: B018
    except ZeroDivisionError:
        logger.exception("Hello, Bob!")

    return 5


def assert_logs(logs: str) -> None:
    """Assert logs follow the flat JSON record structure."""
    (
        info,
        warning,
        error,
        exception,
    ) = (parse_json_log(line) for line in logs.splitlines()[0:4])

    expected_separator = 2

    assert info["logger"] == "tests.logging.test_loguru"
    assert info["caller"]
    assert info["caller"].startswith("generate_logs:")
    assert len(info["caller"].split(":")) == expected_separator
    assert info["time"] == datetime.fromisoformat(info["time"]).isoformat()
    assert info["level"] == "INFO"
    assert info["msg"] == "Hello, World!"
    assert "thread" not in info
    assert "ctx" not in info

    assert warning["logger"] == "tests.logging.test_loguru"
    assert warning["caller"]
    assert warning["caller"].startswith("generate_logs:")
    assert len(warning["caller"].split(":")) == expected_separator
    assert (
        warning["time"] == datetime.fromisoformat(warning["time"]).isoformat()
    )
    assert warning["level"] == "WARNING"
    assert warning["msg"] == "Hello, World!"
    assert "thread" not in warning
    assert "ctx" not in warning

    assert error["logger"] == "tests.logging.test_loguru"
    assert error["caller"]
    assert error["caller"].startswith("generate_logs:")
    assert len(error["caller"].split(":")) == expected_separator
    assert error["time"] == datetime.fromisoformat(error["time"]).isoformat()
    assert error["level"] == "ERROR"
    assert error["msg"] == "Hello, Alice!"
    assert "thread" not in error
    # Extra context is flat at top level
    assert error["user"] == "Alice"

    assert exception["logger"] == "tests.logging.test_loguru"
    assert exception["caller"]
    assert exception["caller"].startswith("generate_logs:")
    assert len(exception["caller"].split(":")) == expected_separator
    assert (
        exception["time"]
        == datetime.fromisoformat(exception["time"]).isoformat()
    )
    assert exception["level"] == "ERROR"
    assert exception["msg"] == "Hello, Bob!"
    assert "thread" not in exception
    assert exception["error"]["type"] == "ZeroDivisionError"
    assert exception["error"]["message"] == "division by zero"


class TestJsonFormatter:
    """Test loguru _json_formatter function."""

    def test_json_formatter(self) -> None:
        """Test JSON Formatter produces valid output."""
        # Arrange
        sink = StringIO()
        patcher = _LoguruPatcher(enable_json=True, enable_caller=True)

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        generate_logs()

        # Assert
        assert_logs(sink.getvalue())

    def test_json_formatter_with_timezone(self) -> None:
        """Test JSON Formatter with explicit timezone via patcher."""
        # Arrange
        sink = StringIO()
        timezone = ZoneInfo("Europe/Paris")
        patcher = _LoguruPatcher(
            timezone=timezone, enable_json=True, enable_caller=True
        )

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        generate_logs()

        # Assert
        assert_logs(sink.getvalue())


class TestJsonPatcher:
    """Test loguru _json_patcher function."""

    def test_json_patching(self) -> None:
        """Test JSON Patching produces valid output."""
        # Arrange
        sink = StringIO()

        def patcher(record: Record) -> None:
            _json_patcher(record, caller_enabled=True)

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=lambda _: JSON_FORMAT + "\n", level="INFO")
        generate_logs()

        # Assert
        assert_logs(sink.getvalue())


class TestOtelPatcher:
    """Test loguru _otel_patcher function."""

    def test_otel_patcher_without_opentelemetry(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _otel_patcher when OpenTelemetry is not installed."""
        # Arrange
        mocker.patch(
            "grelmicro.log._loguru.get_otel_trace_context",
            return_value={},
        )
        sink = StringIO()
        patcher = _LoguruPatcher(enable_otel=True, enable_json=True)

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test without OpenTelemetry", user_id=_USER_ID)

        # Assert
        log_record = parse_json_log(sink.getvalue())

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "Test without OpenTelemetry"
        assert log_record["user_id"] == _USER_ID

    def test_otel_patcher_with_invalid_span(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _otel_patcher when span context is invalid."""
        # Arrange
        mocker.patch(
            "grelmicro.log._loguru.get_otel_trace_context",
            return_value={},
        )
        sink = StringIO()
        patcher = _LoguruPatcher(enable_otel=True, enable_json=True)

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test with invalid span", user_id=_USER_ID_2)

        # Assert
        log_record = parse_json_log(sink.getvalue())

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "Test with invalid span"
        assert log_record["user_id"] == _USER_ID_2

    def test_otel_patcher_with_valid_span(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _otel_patcher with valid OpenTelemetry span."""
        # Arrange
        mocker.patch(
            "grelmicro.log._loguru.get_otel_trace_context",
            return_value={
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "span_id": "00f067aa0ba902b7",
            },
        )
        sink = StringIO()
        patcher = _LoguruPatcher(enable_otel=True, enable_json=True)

        # Act
        logger.configure(patcher=patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test with valid span", user_id=_USER_ID_3)

        # Assert
        log_record = parse_json_log(sink.getvalue())

        assert log_record["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert log_record["span_id"] == "00f067aa0ba902b7"
        assert log_record["msg"] == "Test with valid span"
        assert log_record["user_id"] == _USER_ID_3


class TestLoguruSpecificFeatures:
    """Test loguru-specific features not available in other backends."""

    def test_configure_logging_text_with_traceback(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test TEXT format includes traceback for exceptions."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_FORMAT", "text")
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")

        # Act
        configure()
        generate_logs()

        # Assert
        lines = capsys.readouterr().out.splitlines()

        assert "tests.logging.test_loguru:generate_logs:" in lines[0]
        assert "INFO" in lines[0]
        assert "Hello, World!" in lines[0]

        assert "tests.logging.test_loguru:generate_logs:" in lines[1]
        assert "WARNING" in lines[1]
        assert "Hello, World!" in lines[1]

        assert "tests.logging.test_loguru:generate_logs:" in lines[2]
        assert "ERROR" in lines[2]
        assert "Hello, Alice!" in lines[2]

        assert "tests.logging.test_loguru:generate_logs:" in lines[3]
        assert "ERROR" in lines[3]
        assert "Hello, Bob!" in lines[3]

    def test_configure_logging_custom_format_template(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test custom loguru format template."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_FORMAT", "{level}: {message}")

        # Act
        configure()
        generate_logs()

        # Assert
        lines = capsys.readouterr().out.splitlines()
        assert "INFO: Hello, World!" in lines[0]
        assert "WARNING: Hello, World!" in lines[1]
        assert "ERROR: Hello, Alice!" in lines[2]
        assert "ERROR: Hello, Bob!" in lines[3]
        assert "Traceback" in lines[4]
        assert "ZeroDivisionError: division by zero" in lines[-1]

    def test_configure_logging_custom_format_with_localtime(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test custom format using extra[localtime] triggers localtime patcher."""
        # Arrange
        monkeypatch.setenv(
            "GREL_LOG_FORMAT", "{extra[localtime]} | {level} | {message}"
        )
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        logger.info("Localtime test")

        # Assert
        output = capsys.readouterr().out.strip()
        assert "Localtime test" in output
        assert "INFO" in output
        # localtime format: YYYY-MM-DD HH:MM:SS.mmm
        assert len(output.split(" | ")[0]) >= len("2026-04-01 10:00:00.000")

    def test_configure_logging_simple_format_no_patcher(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test simple format that doesn't need patcher."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_FORMAT", "{level}: {message}")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        logger.info("Simple test")

        # Assert
        assert capsys.readouterr().out.strip() == "INFO: Simple test"

    def test_exception_captured_as_structured(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test exception is captured as structured ErrorDict."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            logger.exception("Division error")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)

        assert log_record["error"]["type"] == "ZeroDivisionError"
        assert log_record["error"]["message"] == "division by zero"
