"""Test Logging Loguru."""

from collections.abc import Generator
from datetime import datetime
from io import StringIO

import pytest
from loguru import logger
from pydantic import TypeAdapter

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging.errors import LoggingSettingsValidationError
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
    """Assert logs."""
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


def test_json_formatter() -> None:
    """Test JSON Formatter."""
    # Arrange
    sink = StringIO()

    # Act
    logger.add(sink, format=json_formatter, level="INFO")
    generate_logs()

    # Assert
    assert_logs(sink.getvalue())


def test_json_patching() -> None:
    """Test JSON Patching."""
    # Arrange
    sink = StringIO()

    # Act
    # logger.patch(json_patcher) -> Patch is not working using logger.configure instead
    logger.configure(patcher=json_patcher)
    logger.add(sink, format=JSON_FORMAT, level="INFO")
    generate_logs()

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
    generate_logs()

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


def test_configure_logging_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging JSON."""
    # Arrange
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_FORMAT", "json")

    # Act
    configure_logging()
    generate_logs()

    # Assert
    assert_logs(capsys.readouterr().out)


def test_configure_logging_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Level."""
    # Arrange
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    # Act
    configure_logging()
    logs_count = generate_logs()

    # Assert
    assert len(capsys.readouterr().out.splitlines()) == logs_count


def test_configure_logging_invalid_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Configure Logging Invalid Level."""
    # Arrange
    monkeypatch.setenv("LOG_LEVEL", "INVALID")
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    # Act
    with pytest.raises(
        LoggingSettingsValidationError,
        match=(
            r"Could not validate environment variables settings:\n"
            r"- LOG_LEVEL: Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'"
            r" \[input=INVALID\]"
        ),
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
    generate_logs()

    # Assert
    lines = capsys.readouterr().out.splitlines()
    assert "INFO: Hello, World!" in lines[0]
    assert "WARNING: Hello, World!" in lines[1]
    assert "ERROR: Hello, Alice!" in lines[2]
    assert "ERROR: Hello, Bob!" in lines[3]
    assert "Traceback" in lines[4]
    assert "ZeroDivisionError: division by zero" in lines[-1]


def test_configure_logging_dependency_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Configure Logging Dependency Not Found."""
    # Arrange
    monkeypatch.setattr("grelmicro.logging.loguru.loguru", None)

    # Act / Assert
    with pytest.raises(DependencyNotFoundError, match="loguru"):
        configure_logging()
