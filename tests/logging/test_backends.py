"""Parametrized Component Tests for Logging Backends.

These tests verify that loguru, structlog, and stdlib backends behave
identically for the public API.
"""

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_mock
import structlog
from loguru import logger as loguru_logger
from pydantic import ValidationError

from grelmicro._json import json_default
from grelmicro.errors import DependencyNotFoundError
from grelmicro.log import configure
from grelmicro.log._shared import (
    _logfmt_format_value,
    get_otel_trace_context,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
    should_colorize,
)
from grelmicro.log._structlog import _add_caller_info
from tests.logging.conftest import BACKENDS, log_message, parse_json_log

_USER_ID = 123
_USER_ID_2 = 456
_USER_ID_3 = 789
_COUNT = 42


class TestConfigureLoggingJSON:
    """Test JSON logging configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_json_format_default(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test default JSON format produces valid log record."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.delenv("GREL_LOG_LEVEL", raising=False)
        monkeypatch.delenv("GREL_LOG_FORMAT", raising=False)
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "Test message", user_id=_USER_ID)

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)

        # Core fields
        assert "time" in log_record
        assert "level" in log_record
        assert "logger" in log_record
        assert "caller" in log_record
        assert "msg" in log_record

        # Values
        assert log_record["level"] == "INFO"
        assert log_record["msg"] == "Test message"

        # Extra context is flat at top level
        assert log_record["user_id"] == _USER_ID

        # Removed fields
        assert "thread" not in log_record
        assert "ctx" not in log_record

        # Time should be valid ISO 8601
        assert (
            log_record["time"]
            == datetime.fromisoformat(log_record["time"]).isoformat()
        )

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_json_format_explicit(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test explicit JSON format setting."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "JSON format test")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "JSON format test"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_json_format_no_context(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test JSON format with no extra context has only core fields."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "No context message")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "No context message"
        assert "ctx" not in log_record

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_json_format_with_context(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test JSON format includes context fields flat at top level."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(
            backend,
            "Context message",
            user_id=_USER_ID,
            action="login",
            ip="192.168.1.1",
        )

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["user_id"] == _USER_ID
        assert log_record["action"] == "login"
        assert log_record["ip"] == "192.168.1.1"


class TestConfigureLoggingLevel:
    """Test log level configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_log_level_warning(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test log level filtering."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        if backend == "loguru":
            loguru_logger.debug("Debug message")
            loguru_logger.info("Info message")
            loguru_logger.warning("Warning message")
        elif backend == "structlog":
            log = structlog.get_logger()
            log.debug("Debug message")
            log.info("Info message")
            log.warning("Warning message")
        else:
            stdlib_logger = logging.getLogger(__name__)
            stdlib_logger.debug("Debug message")
            stdlib_logger.info("Info message")
            stdlib_logger.warning("Warning message")

        # Assert
        output = capsys.readouterr().out
        lines = [line for line in output.strip().split("\n") if line]
        assert len(lines) == 1  # Only warning should be logged

        log_record = parse_json_log(lines[0])
        assert log_record["level"] == "WARNING"
        assert log_record["msg"] == "Warning message"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_invalid_log_level(
        self,
        backend: str,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test invalid log level raises error."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_LEVEL", "INVALID")

        # Act / Assert
        with pytest.raises(
            ValidationError,
            match=(
                r"Input should be 'DEBUG', 'INFO', 'WARNING', "
                r"'ERROR' or 'CRITICAL'"
            ),
        ):
            configure()


class TestConfigureLoggingTimezone:
    """Test timezone configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_timezone_europe_zurich(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test timezone setting affects timestamp."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_TIMEZONE", "Europe/Zurich")
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "Timezone test")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)

        # Should have timezone offset (not Z for UTC)
        time_str = log_record["time"]
        assert datetime.fromisoformat(time_str) is not None
        # Europe/Zurich is UTC+1 or UTC+2 depending on DST
        assert "+01:00" in time_str or "+02:00" in time_str


class TestConfigureLoggingOTel:
    """Test OpenTelemetry integration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_otel_enabled_with_trace_context(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        mocker: pytest_mock.MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test OpenTelemetry trace context is included when enabled."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")

        trace_context = {
            "trace_id": "1234567890abcdef1234567890abcdef",
            "span_id": "1234567890abcdef",
        }

        # Mock the appropriate module
        if backend == "loguru":
            module_path = "grelmicro.log._loguru"
        elif backend == "structlog":
            module_path = "grelmicro.log._structlog"
        else:
            module_path = "grelmicro.log._stdlib"
        mocker.patch(
            f"{module_path}.get_otel_trace_context",
            return_value=trace_context,
        )

        # Act
        configure()
        log_message(backend, "OTel test", request_id="abc-123")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)

        # Trace fields should be at root level
        assert log_record["trace_id"] == trace_context["trace_id"]
        assert log_record["span_id"] == trace_context["span_id"]
        assert log_record["msg"] == "OTel test"
        # Extra context is flat at top level
        assert log_record["request_id"] == "abc-123"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_otel_disabled(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        mocker: pytest_mock.MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test OpenTelemetry trace context is not included when disabled."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")

        # Mock should not be called
        if backend == "loguru":
            module_path = "grelmicro.log._loguru"
        elif backend == "structlog":
            module_path = "grelmicro.log._structlog"
        else:
            module_path = "grelmicro.log._stdlib"
        mock_otel = mocker.patch(f"{module_path}.get_otel_trace_context")

        # Act
        configure()
        log_message(backend, "OTel disabled test", request_id="xyz-456")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "OTel disabled test"
        # Extra context is flat at top level
        assert log_record["request_id"] == "xyz-456"

        # Verify get_otel_trace_context was never called
        mock_otel.assert_not_called()

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_otel_dependency_not_found(
        self,
        backend: str,
        mocker: pytest_mock.MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test error when OTel enabled but not installed."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "true")

        mocker.patch(
            "grelmicro.log._shared.has_opentelemetry", return_value=False
        )

        # Act / Assert
        with pytest.raises(DependencyNotFoundError, match="opentelemetry"):
            configure()


class TestConfigureLoggingText:
    """Test TEXT format configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_text_format(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test TEXT format produces human-readable output."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "text")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "Text format test")

        # Assert
        output = capsys.readouterr().out
        assert "Text format test" in output
        # Both backends should indicate INFO level
        assert "info" in output.lower()


class TestConfigureLoggingJSONSerializer:
    """Test JSON serializer configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_default_stdlib_serializer(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test default stdlib JSON serializer works."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.delenv("GREL_LOG_JSON_SERIALIZER", raising=False)

        # Act
        configure()
        log_message(backend, "Stdlib serializer test", count=_COUNT)

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "Stdlib serializer test"
        assert log_record["count"] == _COUNT
        # Verify time is valid ISO 8601 string
        parsed = datetime.fromisoformat(log_record["time"])
        assert parsed.tzinfo is not None

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_orjson_serializer(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test orjson serializer produces valid output."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("GREL_LOG_JSON_SERIALIZER", "orjson")

        # Act
        configure()
        log_message(backend, "Orjson serializer test", count=_COUNT)

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "Orjson serializer test"
        assert log_record["count"] == _COUNT
        # Verify time is valid ISO 8601 string
        parsed = datetime.fromisoformat(log_record["time"])
        assert parsed.tzinfo is not None

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_orjson_serializer_not_installed(
        self,
        backend: str,
        mocker: pytest_mock.MockerFixture,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test error when orjson serializer is configured but not installed."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_JSON_SERIALIZER", "orjson")

        mocker.patch("grelmicro.log._shared.has_orjson", return_value=False)

        # Act / Assert
        with pytest.raises(DependencyNotFoundError, match="orjson"):
            configure()


class TestLoadSettings:
    """Test load_settings from _shared module."""

    def test_invalid_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test load_settings raises on invalid env var."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_LEVEL", "INVALID")

        # Act / Assert
        with pytest.raises(ValidationError):
            load_settings()


class TestJsonDefault:
    """Test json_default handler for stdlib json."""

    def test_serializes_datetime(self) -> None:
        """Test datetime is serialized to ISO 8601."""
        # Arrange
        dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        # Act / Assert
        assert json_default(dt) == "2024-06-01T12:00:00+00:00"

    def test_unsupported_type_raises(self) -> None:
        """Test non-datetime types raise TypeError."""
        # Act / Assert
        with pytest.raises(TypeError, match="not JSON serializable"):
            json_default(object())


class TestGetOtelTraceContext:
    """Test get_otel_trace_context from _shared module."""

    def test_no_active_span(self) -> None:
        """Test returns empty dict when no active span."""
        # Act - no active span, span_context.is_valid is False
        result = get_otel_trace_context()

        # Assert
        assert result == {}

    def test_trace_not_installed(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Test returns empty dict when opentelemetry is not installed."""
        # Arrange
        mocker.patch("grelmicro.log._shared.trace", None)

        # Act
        result = get_otel_trace_context()

        # Assert
        assert result == {}

    def test_with_active_span(self, mocker: pytest_mock.MockerFixture) -> None:
        """Test returns trace_id and span_id with active span."""
        # Arrange
        mock_span_context = MagicMock()
        mock_span_context.is_valid = True
        mock_span_context.trace_id = 0x1234567890ABCDEF1234567890ABCDEF
        mock_span_context.span_id = 0x1234567890ABCDEF

        mock_span = MagicMock()
        mock_span.get_span_context.return_value = mock_span_context

        mocker.patch(
            "grelmicro.log._shared.trace.get_current_span",
            return_value=mock_span,
        )

        # Act
        result = get_otel_trace_context()

        # Assert
        assert result == {
            "trace_id": "1234567890abcdef1234567890abcdef",
            "span_id": "1234567890abcdef",
        }


class TestStructlogCallerInfo:
    """Test structlog _add_caller_info processor edge cases."""

    def test_with_stdlib_record(self) -> None:
        """Test _add_caller_info when stdlib record is present."""
        # Arrange
        record = MagicMock()
        record.name = "mymodule"
        record.funcName = "myfunc"
        record.lineno = 42
        event_dict: dict[str, object] = {"_record": record, "event": "test"}
        processor = _add_caller_info(caller_enabled=True)

        # Act
        result: dict[str, object] = processor(None, "info", event_dict)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

        # Assert
        assert result["logger"] == "mymodule"
        assert result["caller"] == "myfunc:42"

    def test_with_caller_disabled(self) -> None:
        """Test _add_caller_info sets logger but omits caller when disabled."""
        # Arrange
        record = MagicMock()
        record.name = "mymodule"
        record.funcName = "myfunc"
        record.lineno = 42
        event_dict: dict[str, object] = {"_record": record, "event": "test"}
        processor = _add_caller_info(caller_enabled=False)

        # Act
        result: dict[str, object] = processor(None, "info", event_dict)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

        # Assert
        assert result["logger"] == "mymodule"
        assert "caller" not in result

    def test_without_callsite_info(self) -> None:
        """Test _add_caller_info when no callsite info is available."""
        # Arrange
        event_dict: dict[str, object] = {"event": "test"}
        processor = _add_caller_info(caller_enabled=True)

        # Act
        result: dict[str, object] = processor(None, "info", event_dict)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

        # Assert
        assert result["logger"] == "unknown"
        assert "caller" not in result


_TEST_ERROR_MSG = "test error"


def _raise_value_error() -> None:
    raise ValueError(_TEST_ERROR_MSG)


class TestConfigureLoggingException:
    """Test exception handling across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_exception_produces_structured_error(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test all backends produce structured ErrorDict for exceptions."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        configure()

        # Act
        try:
            _raise_value_error()
        except ValueError:
            if backend == "loguru":
                loguru_logger.exception("Something failed")
            elif backend == "structlog":
                log = structlog.get_logger()
                log.exception("Something failed")
            else:
                stdlib_logger = logging.getLogger("test_exception")
                stdlib_logger.exception("Something failed")

        # Assert
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "Something failed"
        assert log_record["error"]["type"] == "ValueError"
        assert log_record["error"]["message"] == _TEST_ERROR_MSG
        assert "stack" in log_record["error"]
        assert "ValueError: test error" in log_record["error"]["stack"]


class TestCoreFieldCollisionProtection:
    """Test that user extras cannot overwrite core fields."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_user_extras_cannot_overwrite_core_fields(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test core fields are protected from user-supplied extras."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "json")
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        configure()

        # Act - attempt to overwrite core fields via extras
        log_message(
            backend,
            "Real message",
            level="FAKE",
            time="1970-01-01",
            caller="evil:inject:0",
        )

        # Assert - core fields should NOT be overwritten
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["level"] == "INFO"
        assert log_record["msg"] == "Real message"
        assert log_record["time"] != "1970-01-01"
        assert log_record["caller"] != "evil:inject:0"


class TestConfigureLoggingLogfmt:
    """Test LOGFMT format configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_logfmt_format(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test LOGFMT format produces key=value output."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "logfmt")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "Logfmt test", user_id=_USER_ID)

        # Assert
        output = capsys.readouterr().out.strip()
        assert "level=INFO" in output
        assert 'msg="Logfmt test"' in output
        assert f"user_id={_USER_ID}" in output
        assert "time=" in output
        assert "logger=" in output

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_logfmt_format_no_extras(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test LOGFMT format with no extra context."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "logfmt")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "No extras")

        # Assert
        output = capsys.readouterr().out.strip()
        assert "level=INFO" in output
        assert 'msg="No extras"' in output
        assert "time=" in output
        assert "logger=" in output

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_logfmt_format_with_special_chars(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test LOGFMT format quotes values with spaces."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "logfmt")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act
        configure()
        log_message(backend, "Special test", path="/api/hello world")

        # Assert
        output = capsys.readouterr().out.strip()
        assert 'path="/api/hello world"' in output


class TestConfigureLoggingPretty:
    """Test PRETTY format configuration across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_pretty_format(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test PRETTY format produces multi-line output."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "pretty")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Pretty test", user_id=_USER_ID)

        # Assert
        output = capsys.readouterr().out
        lines = output.strip().splitlines()
        # Multi-line: header, "at" line, at least one extra field
        _min_pretty_lines = 3
        assert len(lines) >= _min_pretty_lines
        assert "Pretty test" in lines[0]
        assert "INFO" in lines[0]
        assert "at " in lines[1]
        # Extra field on its own line
        assert any(f"user_id: {_USER_ID}" in line for line in lines)

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_pretty_format_no_extras(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test PRETTY format with no extra context."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "pretty")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "No extras pretty")

        # Assert
        output = capsys.readouterr().out
        lines = output.strip().splitlines()
        _min_pretty_lines = 2
        assert len(lines) >= _min_pretty_lines
        assert "No extras pretty" in lines[0]
        assert "at " in lines[1]


class TestConfigureLoggingAuto:
    """Test AUTO format resolution."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_auto_non_tty_resolves_to_json(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test AUTO format resolves to JSON when stdout is not a TTY."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "auto")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act (test runner stdout is not a TTY)
        configure()
        log_message(backend, "Auto test")

        # Assert - should produce valid JSON
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "Auto test"
        assert log_record["level"] == "INFO"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_default_format_is_auto(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test default format (no GREL_LOG_FORMAT set) behaves as AUTO."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.delenv("GREL_LOG_FORMAT", raising=False)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

        # Act (test runner stdout is not a TTY, so AUTO -> JSON)
        configure()
        log_message(backend, "Default format test")

        # Assert - should produce valid JSON
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "Default format test"


class TestLogfmtSerializer:
    """Test logfmt serialization logic."""

    def test_simple_values(self) -> None:
        """Test logfmt with simple string and numeric values."""
        record = {
            "time": "2026-04-01T10:00:00+00:00",
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
        }
        result = logfmt_dumps(record)
        assert "time=2026-04-01T10:00:00+00:00" in result
        assert "level=INFO" in result
        assert "msg=hello" in result
        assert "logger=mod" in result
        assert "caller=fn:1" in result

    def test_value_with_spaces_is_quoted(self) -> None:
        """Test logfmt quotes values containing spaces."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "hello world",
            "logger": "l",
            "caller": "c",
        }
        result = logfmt_dumps(record)
        assert 'msg="hello world"' in result

    def test_nested_dict_uses_dot_notation(self) -> None:
        """Test logfmt flattens nested dicts with dot notation."""
        record = {
            "time": "t",
            "level": "ERROR",
            "msg": "fail",
            "logger": "l",
            "caller": "c",
            "error": {"type": "ValueError", "message": "bad input"},
        }
        result = logfmt_dumps(record)
        assert "error.type=ValueError" in result
        assert 'error.message="bad input"' in result

    def test_none_values_omitted(self) -> None:
        """Test logfmt omits None values."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "hi",
            "logger": "l",
            "caller": "c",
            "extra": None,
        }
        result = logfmt_dumps(record)
        assert "extra" not in result

    def test_none_value(self) -> None:
        """Test logfmt formats None as empty string."""
        assert _logfmt_format_value(value=None) == ""

    def test_boolean_values(self) -> None:
        """Test logfmt formats booleans as lowercase."""
        assert _logfmt_format_value(value=True) == "true"
        assert _logfmt_format_value(value=False) == "false"

    def test_empty_string_quoted(self) -> None:
        """Test logfmt quotes empty strings."""
        assert _logfmt_format_value("") == '""'

    def test_value_with_quotes_escaped(self) -> None:
        """Test logfmt escapes quotes in values."""
        assert _logfmt_format_value('say "hello"') == '"say \\"hello\\""'

    def test_core_fields_come_first(self) -> None:
        """Test core fields appear before extras in logfmt output."""
        record = {
            "extra_field": "val",
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "l",
            "caller": "c",
        }
        result = logfmt_dumps(record)
        time_pos = result.index("time=")
        extra_pos = result.index("extra_field=")
        assert time_pos < extra_pos

    def test_missing_caller_omitted(self) -> None:
        """Test logfmt output is valid when caller is absent."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "uvicorn.error",
        }
        result = logfmt_dumps(record)
        assert "caller" not in result
        assert "logger=uvicorn.error" in result

    def test_none_core_field_omitted(self) -> None:
        """Test logfmt omits None-valued core fields."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "l",
            "caller": "c",
            "trace_id": None,
        }
        result = logfmt_dumps(record)
        assert "trace_id" not in result

    def test_nested_dict_in_extras(self) -> None:
        """Test logfmt flattens nested dicts in extra fields."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "l",
            "caller": "c",
            "meta": {"region": "eu", "env": "prod"},
        }
        result = logfmt_dumps(record)
        assert "meta.region=eu" in result
        assert "meta.env=prod" in result

    def test_deeply_nested_dict(self) -> None:
        """Test logfmt flattens deeply nested dicts with dot notation."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "l",
            "caller": "c",
            "http": {"request": {"method": "GET", "path": "/api"}},
        }
        result = logfmt_dumps(record)
        assert "http.request.method=GET" in result
        assert "http.request.path=/api" in result

    def test_none_in_nested_dict_omitted(self) -> None:
        """Test logfmt omits None values inside nested dicts."""
        record = {
            "time": "t",
            "level": "INFO",
            "msg": "m",
            "logger": "l",
            "caller": "c",
            "error": {"type": "ValueError", "stack": None},
        }
        result = logfmt_dumps(record)
        assert "error.type=ValueError" in result
        assert "stack" not in result


class TestShouldColorize:
    """Test color detection logic."""

    def test_force_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test FORCE_COLOR forces colors on."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert should_colorize() is True

    def test_no_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test NO_COLOR disables colors."""
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        assert should_colorize() is False

    def test_force_color_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test FORCE_COLOR takes precedence over NO_COLOR."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("NO_COLOR", "1")
        assert should_colorize() is True

    def test_non_tty_no_colors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test non-TTY stream produces no colors."""
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        # Test runner stdout is not a TTY
        assert should_colorize() is False


class TestRenderTextLine:
    """Test shared text line renderer."""

    def test_basic_render(self) -> None:
        """Test basic text line rendering without colors."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
        }
        result = render_text_line(record, colors=False)
        assert "2026-04-01 10:30:00.000" in result
        assert "INFO" in result
        assert "mod:fn:1" in result
        assert "hello" in result

    def test_with_extras(self) -> None:
        """Test text line includes extras as key=value."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
            "user_id": 123,
        }
        result = render_text_line(record, colors=False)
        assert "user_id=123" in result

    def test_missing_caller_shows_logger_only(self) -> None:
        """Test text line uses logger as source when caller is absent."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "uvicorn.error",
        }
        result = render_text_line(record, colors=False)
        assert "uvicorn.error" in result
        assert "uvicorn.error:" not in result

    def test_with_colors(self) -> None:
        """Test text line includes ANSI codes when colors enabled."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
        }
        result = render_text_line(record, colors=True)
        assert "\033[" in result  # ANSI escape codes present


class TestRenderPrettyLines:
    """Test shared pretty renderer."""

    def test_basic_render(self) -> None:
        """Test basic pretty rendering without colors."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
        }
        result = render_pretty_lines(record, colors=False)
        lines = result.splitlines()
        assert "INFO" in lines[0]
        assert "hello" in lines[0]
        assert "at mod:fn:1" in lines[1]

    def test_missing_caller_shows_logger_only(self) -> None:
        """Test pretty rendering uses logger as source when caller is absent."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "uvicorn.error",
        }
        result = render_pretty_lines(record, colors=False)
        lines = result.splitlines()
        assert "at uvicorn.error" in lines[1]
        assert "at uvicorn.error:" not in lines[1]

    def test_with_extras(self) -> None:
        """Test pretty rendering includes extras on separate lines."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
            "user_id": 123,
        }
        result = render_pretty_lines(record, colors=False)
        assert "user_id: 123" in result

    def test_with_error(self) -> None:
        """Test pretty rendering includes error fields."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "ERROR",
            "msg": "fail",
            "logger": "mod",
            "caller": "fn:1",
            "error": {
                "type": "ValueError",
                "message": "bad",
                "stack": "line1\nline2",
            },
        }
        result = render_pretty_lines(record, colors=False)
        assert "error.type: ValueError" in result
        assert "error.message: bad" in result
        assert "error.stack:" in result
        assert "line1" in result
        assert "line2" in result

    def test_with_colors(self) -> None:
        """Test pretty rendering includes ANSI codes when colors enabled."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
            "user_id": 42,
        }
        result = render_pretty_lines(record, colors=True)
        assert "\033[" in result
        assert "user_id:" in result

    def test_with_trace_context(self) -> None:
        """Test pretty rendering includes trace_id and span_id."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "INFO",
            "msg": "hello",
            "logger": "mod",
            "caller": "fn:1",
            "trace_id": "abc123",
            "span_id": "def456",
        }
        result = render_pretty_lines(record, colors=False)
        assert "trace_id: abc123" in result
        assert "span_id: def456" in result

    def test_with_error_and_colors(self) -> None:
        """Test pretty error rendering with ANSI colors."""
        record = {
            "time": datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC),
            "level": "ERROR",
            "msg": "fail",
            "logger": "mod",
            "caller": "fn:1",
            "error": {
                "type": "ValueError",
                "message": "bad",
                "stack": "line1\nline2",
            },
        }
        result = render_pretty_lines(record, colors=True)
        assert "\033[31m" in result  # Red for stack lines
        assert "error.type:" in result
        assert "error.stack:" in result


_NON_JSON_FORMATS = ["text", "logfmt", "pretty"]


class TestAllFormatsException:
    """Test exception handling across logfmt and pretty formats."""

    @pytest.mark.parametrize("backend", BACKENDS)
    @pytest.mark.parametrize("fmt", ["logfmt", "pretty"])
    def test_exception_appears_in_output(
        self,
        backend: str,
        fmt: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test all formats include exception info."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", fmt)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        configure()

        # Act
        try:
            _raise_value_error()
        except ValueError:
            if backend == "loguru":
                loguru_logger.exception("Something failed")
            elif backend == "structlog":
                log = structlog.get_logger()
                log.exception("Something failed")
            else:
                stdlib_logger = logging.getLogger("test_exc")
                stdlib_logger.exception("Something failed")

        # Assert
        output = capsys.readouterr().out
        assert "Something failed" in output

        if fmt == "logfmt":
            assert "error.type=ValueError" in output
            assert f'error.message="{_TEST_ERROR_MSG}"' in output
        elif fmt == "pretty":
            assert "error.type: ValueError" in output
            assert f"error.message: {_TEST_ERROR_MSG}" in output
            assert "error.stack:" in output


class TestAllFormatsTimezone:
    """Test timezone across all formats and backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    @pytest.mark.parametrize("fmt", _NON_JSON_FORMATS)
    def test_timezone_applied(
        self,
        backend: str,
        fmt: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test all formats respect GREL_LOG_TIMEZONE."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", fmt)
        monkeypatch.setenv("GREL_LOG_TIMEZONE", "Europe/Zurich")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Timezone test")

        # Assert
        output = capsys.readouterr().out
        assert "Timezone test" in output

        if fmt == "logfmt":
            # logfmt time has ISO 8601 offset
            assert "+01:00" in output or "+02:00" in output
        else:
            # text/pretty have local time in the output
            assert "Timezone test" in output


class TestAllFormatsCaller:
    """Test caller info across all formats and backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    @pytest.mark.parametrize("fmt", _NON_JSON_FORMATS)
    def test_caller_present(
        self,
        backend: str,
        fmt: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test all formats include caller info."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", fmt)
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Caller test")

        # Assert
        output = capsys.readouterr().out
        # All formats should contain module:function:line pattern
        assert "log_message:" in output


class TestAllFormatsMultipleExtras:
    """Test multiple extras across all formats and backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    @pytest.mark.parametrize("fmt", _NON_JSON_FORMATS)
    def test_multiple_extras(
        self,
        backend: str,
        fmt: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test all formats include multiple extra context fields."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", fmt)
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(
            backend,
            "Multi extras",
            user_id=_USER_ID,
            action="login",
        )

        # Assert
        output = capsys.readouterr().out
        assert "Multi extras" in output
        assert str(_USER_ID) in output
        assert "login" in output


class TestPrettyFormatStructure:
    """Test PRETTY format detailed structure across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_pretty_caller_on_at_line(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test PRETTY format has caller on 'at' line."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "pretty")
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Structure test")

        # Assert
        lines = capsys.readouterr().out.strip().splitlines()
        assert "at " in lines[1]
        assert "log_message:" in lines[1]

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_pretty_extras_on_separate_lines(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test PRETTY format puts each extra on its own indented line."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "pretty")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Fields test", user_id=_USER_ID, count=_COUNT)

        # Assert
        lines = capsys.readouterr().out.strip().splitlines()
        extra_lines = [
            line
            for line in lines
            if line.startswith("    ") and "at " not in line
        ]
        assert any(f"user_id: {_USER_ID}" in line for line in extra_lines)
        assert any(f"count: {_COUNT}" in line for line in extra_lines)

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_pretty_exception_structure(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test PRETTY format renders full error block."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "pretty")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        configure()

        # Act
        try:
            _raise_value_error()
        except ValueError:
            if backend == "loguru":
                loguru_logger.exception("Crashed")
            elif backend == "structlog":
                log = structlog.get_logger()
                log.exception("Crashed")
            else:
                stdlib_logger = logging.getLogger("test_pretty_exc")
                stdlib_logger.exception("Crashed")

        # Assert
        output = capsys.readouterr().out
        assert "Crashed" in output
        assert "error.type: ValueError" in output
        assert f"error.message: {_TEST_ERROR_MSG}" in output
        assert "error.stack:" in output
        assert "ValueError: test error" in output


class TestTextFormatStructure:
    """Test TEXT format detailed structure across backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_text_single_line(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test TEXT format produces a single line per log."""
        # Arrange
        monkeypatch.setenv("GREL_LOG_BACKEND", backend)
        monkeypatch.setenv("GREL_LOG_FORMAT", "text")
        monkeypatch.setenv("GREL_LOG_CALLER_ENABLED", "true")
        monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")

        # Act
        configure()
        log_message(backend, "Single line test", user_id=_USER_ID)

        # Assert
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        line = lines[0]
        assert "INFO" in line
        assert "Single line test" in line
        assert f"user_id={_USER_ID}" in line
        assert "log_message:" in line
        assert " - " in line
