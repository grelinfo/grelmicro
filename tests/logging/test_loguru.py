"""Test Logging Loguru."""

from collections.abc import Generator
from datetime import datetime
from io import StringIO

import pytest
from loguru import logger
from pydantic import TypeAdapter

from grelmicro.errors import DependencyNotFoundError, EnvValidationError
from grelmicro.logging.loguru import (
    JSON_FORMAT,
    JSONRecordDict,
    configure_logging,
    json_formatter,
    json_patcher,
)

json_record_type_adapter = TypeAdapter(JSONRecordDict)


@pytest.fixture(autouse=True)
def cleanup_handlers() -> Generator[None, None, None]:
    """Cleanup logging handlers."""
    logger.configure(handlers=[])
    yield
    logger.remove()


def assert_logs(logs: str) -> None:
    """Assert logs."""
    info_json, error_json = logs.splitlines()

    expected_separator = 3

    info = json_record_type_adapter.validate_json(info_json)
    assert info["logger"]
    assert info["logger"].startswith("tests.logging.test_loguru:test_")
    assert len(info["logger"].split(":")) == expected_separator
    assert info["time"] == datetime.fromisoformat(info["time"]).isoformat()
    assert info["level"] == "INFO"
    assert info["msg"] == "Hello, World!"
    assert info["thread"] == "MainThread"
    assert "context" not in info

    error = json_record_type_adapter.validate_json(error_json)
    assert error["logger"]
    assert error["logger"].startswith("tests.logging.test_loguru:test_")
    assert len(error["logger"].split(":")) == expected_separator
    assert error["time"] == datetime.fromisoformat(error["time"]).isoformat()
    assert error["level"] == "ERROR"
    assert error["msg"] == "Hello, Alice!"
    assert error["thread"] == "MainThread"
    assert error["context"] == {"user": "Alice"}


def test_json_formatter() -> None:
    """Test JSON Formatter."""
    # Arrange
    sink = StringIO()

    # Act
    logger.add(sink, format=json_formatter)

    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    assert_logs(sink.getvalue())


def test_json_patching() -> None:
    """Test JSON Patching."""
    # Arrange
    sink = StringIO()

    # Act
    # logger.patch(json_patcher) -> Patch is not working using logger.configure instead
    logger.configure(patcher=json_patcher)
    logger.add(sink, format=JSON_FORMAT)
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    assert_logs(sink.getvalue())


def test_configure_logging_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Default."""
    # Arrange
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    # Act
    configure_logging()
    logger.debug("Hello, World!")
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    assert_logs(capsys.readouterr().out)


def test_configure_logging_text(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Text."""
    # Arrange
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_FORMAT", "text")

    # Act
    configure_logging()
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    info, error = capsys.readouterr().out.splitlines()

    assert "tests.logging.test_loguru:test_configure_logging_text" in info
    assert " | INFO     | " in info
    assert " - Hello, World!" in info

    assert "tests.logging.test_loguru:test_configure_logging_text" in error
    assert " | ERROR    | " in error
    assert " - Hello, Alice!" in error


def test_configure_logging_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging JSON."""
    # Arrange
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_FORMAT", "json")

    # Act
    configure_logging()
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    assert_logs(capsys.readouterr().out)


def test_configure_logging_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Level."""
    # Arrange
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    expected_logs = 3

    # Act
    configure_logging()
    logger.debug("Hello, World!")
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    assert len(capsys.readouterr().out.splitlines()) == expected_logs


def test_configure_logging_invalid_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Invalid Level."""
    # Arrange
    monkeypatch.setenv("LOG_LEVEL", "INVALID")
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    # Act
    with pytest.raises(
        EnvValidationError,
        match="Validation error for env LOG_LEVEL: Level 'INVALID' does not exist",
    ):
        configure_logging()

    # Assert
    assert not capsys.readouterr().out


def test_configure_logging_format_template(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Format Template."""
    # Arrange
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_FORMAT", "{level}: {message}")

    # Act
    configure_logging()
    logger.info("Hello, World!")
    logger.error("Hello, Alice!", user="Alice")

    # Assert
    info, error = capsys.readouterr().out.splitlines()
    assert "INFO: Hello, World!" in info
    assert "ERROR: Hello, Alice!" in error


def test_configure_logging_dependency_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Configure Logging Dependency Not Found."""
    # Arrange
    monkeypatch.setattr("grelmicro.logging.loguru.logger", None)

    # Act / Assert
    with pytest.raises(DependencyNotFoundError, match="loguru"):
        configure_logging()
