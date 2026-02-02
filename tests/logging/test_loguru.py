"""Unit Tests for Loguru Backend.

These tests verify loguru-specific internal implementation details
that are not shared with other backends.
"""

from collections.abc import Generator
from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import pytest_mock
from loguru import logger
from pydantic import TypeAdapter

from grelmicro.logging._loguru import (
    JSON_FORMAT,
    _json_formatter,
    _json_patcher,
    _otel_patcher,
    configure_logging,
)
from grelmicro.logging.types import JSONRecordDict

if TYPE_CHECKING:
    from loguru import Record

json_record_type_adapter = TypeAdapter(JSONRecordDict)


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
    """Assert logs follow JSONRecordDict structure."""
    (
        info,
        warning,
        error,
        exception,
    ) = (
        json_record_type_adapter.validate_json(line)
        for line in logs.splitlines()[0:4]
    )

    expected_separator = 3

    assert info["logger"]
    assert info["logger"].startswith("tests.logging.test_loguru:generate_logs:")
    assert len(info["logger"].split(":")) == expected_separator
    assert info["time"] == datetime.fromisoformat(info["time"]).isoformat()
    assert info["level"] == "INFO"
    assert info["msg"] == "Hello, World!"
    assert info["thread"] == "MainThread"
    assert "ctx" not in info

    assert warning["logger"]
    assert warning["logger"].startswith(
        "tests.logging.test_loguru:generate_logs:"
    )
    assert len(warning["logger"].split(":")) == expected_separator
    assert (
        warning["time"] == datetime.fromisoformat(warning["time"]).isoformat()
    )
    assert warning["level"] == "WARNING"
    assert warning["msg"] == "Hello, World!"
    assert warning["thread"] == "MainThread"
    assert "ctx" not in warning

    assert error["logger"]
    assert error["logger"].startswith(
        "tests.logging.test_loguru:generate_logs:"
    )
    assert len(error["logger"].split(":")) == expected_separator
    assert error["time"] == datetime.fromisoformat(error["time"]).isoformat()
    assert error["level"] == "ERROR"
    assert error["msg"] == "Hello, Alice!"
    assert error["thread"] == "MainThread"
    assert error["ctx"] == {"user": "Alice"}

    assert exception["logger"]
    assert exception["logger"].startswith(
        "tests.logging.test_loguru:generate_logs:"
    )
    assert len(exception["logger"].split(":")) == expected_separator
    assert (
        exception["time"]
        == datetime.fromisoformat(exception["time"]).isoformat()
    )
    assert exception["level"] == "ERROR"
    assert exception["msg"] == "Hello, Bob!"
    assert exception["thread"] == "MainThread"
    assert exception["ctx"] == {
        "exception": "ZeroDivisionError: division by zero",
    }


class TestJsonFormatter:
    """Test loguru _json_formatter function."""

    def test_json_formatter(self) -> None:
        """Test JSON Formatter produces valid output."""
        # Arrange
        sink = StringIO()

        # Act
        logger.add(sink, format=_json_formatter, level="INFO")
        generate_logs()

        # Assert
        assert_logs(sink.getvalue())

    def test_json_formatter_with_timezone(self) -> None:
        """Test JSON Formatter with explicit timezone."""
        # Arrange
        sink = StringIO()
        timezone = ZoneInfo("Europe/Paris")

        def custom_json_formatter(record: "Record") -> str:
            """Return custom JSON formatted log."""
            return _json_formatter(record, timezone=timezone)

        # Act
        logger.add(sink, format=custom_json_formatter, level="INFO")
        generate_logs()

        # Assert
        assert_logs(sink.getvalue())


class TestJsonPatcher:
    """Test loguru _json_patcher function."""

    def test_json_patching(self) -> None:
        """Test JSON Patching produces valid output."""
        # Arrange
        sink = StringIO()

        # Act
        logger.configure(patcher=_json_patcher)
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
            "grelmicro.logging._loguru.get_otel_trace_context",
            return_value={},
        )
        sink = StringIO()

        # Act
        logger.configure(patcher=_otel_patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test without OpenTelemetry", user_id=123)

        # Assert
        log_line = sink.getvalue().strip()
        log_record = json_record_type_adapter.validate_json(log_line)

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "Test without OpenTelemetry"
        assert log_record["ctx"] == {"user_id": 123}

    def test_otel_patcher_with_invalid_span(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _otel_patcher when span context is invalid."""
        # Arrange
        mocker.patch(
            "grelmicro.logging._loguru.get_otel_trace_context",
            return_value={},
        )
        sink = StringIO()

        # Act
        logger.configure(patcher=_otel_patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test with invalid span", user_id=456)

        # Assert
        log_line = sink.getvalue().strip()
        log_record = json_record_type_adapter.validate_json(log_line)

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "Test with invalid span"
        assert log_record["ctx"] == {"user_id": 456}

    def test_otel_patcher_with_valid_span(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _otel_patcher with valid OpenTelemetry span."""
        # Arrange
        mocker.patch(
            "grelmicro.logging._loguru.get_otel_trace_context",
            return_value={
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "span_id": "00f067aa0ba902b7",
            },
        )
        sink = StringIO()

        # Act
        logger.configure(patcher=_otel_patcher)
        logger.add(sink, format=_json_formatter, level="INFO")
        logger.info("Test with valid span", user_id=789)

        # Assert
        log_line = sink.getvalue().strip()
        log_record = json_record_type_adapter.validate_json(log_line)

        assert log_record["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert log_record["span_id"] == "00f067aa0ba902b7"
        assert log_record["msg"] == "Test with valid span"
        assert log_record["ctx"] == {"user_id": 789}


class TestLoguruSpecificFeatures:
    """Test loguru-specific features not available in other backends."""

    def test_configure_logging_text_with_traceback(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test TEXT format includes traceback for exceptions."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "text")

        # Act
        configure_logging()
        generate_logs()

        # Assert
        lines = capsys.readouterr().out.splitlines()

        assert "tests.logging.test_loguru:generate_logs:" in lines[0]
        assert " | INFO     | " in lines[0]
        assert " - Hello, World!" in lines[0]

        assert "tests.logging.test_loguru:generate_logs:" in lines[1]
        assert " | WARNING  | " in lines[1]
        assert " - Hello, World!" in lines[1]

        assert "tests.logging.test_loguru:generate_logs:" in lines[2]
        assert " | ERROR    | " in lines[2]
        assert " - Hello, Alice!" in lines[2]

        assert "tests.logging.test_loguru:generate_logs:" in lines[3]
        assert " | ERROR    | " in lines[3]
        assert " - Hello, Bob!" in lines[3]
        assert "Traceback" in lines[4]
        assert "ZeroDivisionError: division by zero" in lines[-1]

    def test_configure_logging_custom_format_template(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test custom loguru format template."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "{level}: {message}")

        # Act
        configure_logging()
        generate_logs()

        # Assert
        lines = capsys.readouterr().out.splitlines()
        assert "INFO: Hello, World!" in lines[0]
        assert "WARNING: Hello, World!" in lines[1]
        assert "ERROR: Hello, Alice!" in lines[2]
        assert "ERROR: Hello, Bob!" in lines[3]
        assert "Traceback" in lines[4]
        assert "ZeroDivisionError: division by zero" in lines[-1]

    def test_configure_logging_simple_format_no_patcher(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test simple format that doesn't need patcher."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "{level}: {message}")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        logger.info("Simple test")

        # Assert
        assert capsys.readouterr().out.strip() == "INFO: Simple test"

    def test_exception_captured_in_ctx(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test exception is captured in ctx field for JSON format."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            logger.exception("Division error")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)

        assert log_record["ctx"]["exception"] == (
            "ZeroDivisionError: division by zero"
        )
