"""Parametrized Component Tests for Logging Backends.

These tests verify that loguru, structlog, and stdlib backends behave
identically for the public API.
"""

import logging
from collections.abc import Generator
from datetime import datetime

import pytest
import pytest_mock
import structlog
from loguru import logger as loguru_logger
from pydantic import TypeAdapter

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging import configure_logging
from grelmicro.logging.errors import LoggingSettingsValidationError
from grelmicro.logging.types import JSONRecordDict

json_record_type_adapter = TypeAdapter(JSONRecordDict)


# Backend configurations
BACKENDS = ["loguru", "structlog", "stdlib"]


@pytest.fixture
def reset_loguru() -> Generator[None, None, None]:
    """Reset loguru configuration."""
    loguru_logger.configure(handlers=[])
    yield
    loguru_logger.remove()


@pytest.fixture
def reset_structlog() -> Generator[None, None, None]:
    """Reset structlog configuration."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.fixture
def reset_stdlib() -> Generator[None, None, None]:
    """Reset stdlib logging configuration."""
    root = logging.getLogger()
    old_handlers = root.handlers.copy()
    old_level = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(old_handlers)
    root.setLevel(old_level)


@pytest.fixture
def reset_backend(
    reset_loguru: None,
    reset_structlog: None,
    reset_stdlib: None,
) -> None:
    """Reset all backends before each test."""
    _ = reset_loguru, reset_structlog, reset_stdlib


def log_message(backend: str, msg: str, **kwargs: object) -> None:
    """Log a message using the appropriate backend."""
    if backend == "loguru":
        loguru_logger.info(msg, **kwargs)
    elif backend == "structlog":
        log = structlog.get_logger()
        log.info(msg, **kwargs)
    else:
        stdlib_logger = logging.getLogger(__name__)
        stdlib_logger.info(msg, extra=kwargs)


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
        """Test default JSON format produces valid JSONRecordDict."""
        # Arrange
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        log_message(backend, "Test message", user_id=123)

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)

        # Required fields
        assert "time" in log_record
        assert "level" in log_record
        assert "thread" in log_record
        assert "logger" in log_record
        assert "msg" in log_record

        # Values
        assert log_record["level"] == "INFO"
        assert log_record["msg"] == "Test message"
        assert log_record["thread"] == "MainThread"
        assert log_record["ctx"] == {"user_id": 123}

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        log_message(backend, "JSON format test")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)
        assert log_record["msg"] == "JSON format test"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_json_format_no_context(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test JSON format omits ctx when no context provided."""
        # Arrange
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        log_message(backend, "No context message")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)
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
        """Test JSON format includes context fields in ctx."""
        # Arrange
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        log_message(
            backend,
            "Context message",
            user_id=123,
            action="login",
            ip="192.168.1.1",
        )

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)
        assert log_record["ctx"] == {
            "user_id": 123,
            "action": "login",
            "ip": "192.168.1.1",
        }


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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
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

        log_record = json_record_type_adapter.validate_json(lines[0])
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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_LEVEL", "INVALID")

        # Act / Assert
        with pytest.raises(
            LoggingSettingsValidationError,
            match=(
                r"Could not validate environment variables settings:\n"
                r"- LOG_LEVEL: Input should be 'DEBUG', 'INFO', 'WARNING', "
                r"'ERROR' or 'CRITICAL'"
                r" \[input=INVALID\]"
            ),
        ):
            configure_logging()


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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_TIMEZONE", "Europe/Zurich")
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
        log_message(backend, "Timezone test")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_OTEL_ENABLED", "true")
        monkeypatch.setenv("LOG_FORMAT", "json")

        trace_context = {
            "trace_id": "1234567890abcdef1234567890abcdef",
            "span_id": "1234567890abcdef",
        }

        # Mock the appropriate module
        if backend == "loguru":
            module_path = "grelmicro.logging._loguru"
        elif backend == "structlog":
            module_path = "grelmicro.logging._structlog"
        else:
            module_path = "grelmicro.logging._stdlib"
        mocker.patch(
            f"{module_path}.get_otel_trace_context",
            return_value=trace_context,
        )

        # Act
        configure_logging()
        log_message(backend, "OTel test", request_id="abc-123")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)

        # Trace fields should be at root level
        assert log_record["trace_id"] == trace_context["trace_id"]
        assert log_record["span_id"] == trace_context["span_id"]
        assert log_record["msg"] == "OTel test"
        assert log_record["ctx"] == {"request_id": "abc-123"}

        # Trace fields should NOT be in ctx
        assert "trace_id" not in log_record.get("ctx", {})
        assert "span_id" not in log_record.get("ctx", {})

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("LOG_FORMAT", "json")

        # Mock should not be called
        if backend == "loguru":
            module_path = "grelmicro.logging._loguru"
        elif backend == "structlog":
            module_path = "grelmicro.logging._structlog"
        else:
            module_path = "grelmicro.logging._stdlib"
        mock_otel = mocker.patch(f"{module_path}.get_otel_trace_context")

        # Act
        configure_logging()
        log_message(backend, "OTel disabled test", request_id="xyz-456")

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)

        assert "trace_id" not in log_record
        assert "span_id" not in log_record
        assert log_record["msg"] == "OTel disabled test"
        assert log_record["ctx"] == {"request_id": "xyz-456"}

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_OTEL_ENABLED", "true")

        mocker.patch(
            "grelmicro.logging._shared.has_opentelemetry", return_value=False
        )

        # Act / Assert
        with pytest.raises(DependencyNotFoundError, match="opentelemetry"):
            configure_logging()


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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "text")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        # Act
        configure_logging()
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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.delenv("LOG_JSON_SERIALIZER", raising=False)

        # Act
        configure_logging()
        log_message(backend, "Stdlib serializer test", count=42)

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)
        assert log_record["msg"] == "Stdlib serializer test"
        assert log_record["ctx"] == {"count": 42}

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("LOG_JSON_SERIALIZER", "orjson")

        # Act
        configure_logging()
        log_message(backend, "Orjson serializer test", count=42)

        # Assert
        output = capsys.readouterr().out.strip()
        log_record = json_record_type_adapter.validate_json(output)
        assert log_record["msg"] == "Orjson serializer test"
        assert log_record["ctx"] == {"count": 42}

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
        monkeypatch.setenv("LOG_BACKEND", backend)
        monkeypatch.setenv("LOG_JSON_SERIALIZER", "orjson")

        mocker.patch("grelmicro.logging._shared.has_orjson", return_value=False)

        # Act / Assert
        with pytest.raises(DependencyNotFoundError, match="orjson"):
            configure_logging()
