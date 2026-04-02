"""Tests for Uvicorn JSON formatters."""

import json
import logging
import logging.config
import subprocess
import sys
import tempfile
import time
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from grelmicro.logging.uvicorn import (
    UvicornAccessFormatter,
    UvicornFormatter,
)


def _parse(output: str) -> dict[str, Any]:
    return json.loads(output.strip())


@pytest.fixture
def formatter(monkeypatch: pytest.MonkeyPatch) -> UvicornFormatter:
    """Create a UvicornFormatter with default settings."""
    monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
    return UvicornFormatter()


@pytest.fixture
def access_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> UvicornAccessFormatter:
    """Create a UvicornAccessFormatter with default settings."""
    monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
    return UvicornAccessFormatter()


def _make_record(
    name: str = "uvicorn.access",
    msg: str = '%s - "%s %s HTTP/%s" %d',
    args: tuple[Any, ...] | None = (
        "127.0.0.1:8000",
        "GET",
        "/api/v1/users",
        "1.1",
        HTTPStatus.OK,
    ),
    level: int = logging.INFO,
) -> logging.LogRecord:
    """Create a log record matching uvicorn's access log format."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestUvicornFormatter:
    """Test UvicornFormatter for regular uvicorn log messages."""

    def test_formats_simple_message(self, formatter: UvicornFormatter) -> None:
        """Test basic message formatting produces valid JSON with core fields."""
        # Arrange
        record = _make_record(
            name="uvicorn.error",
            msg="Application startup complete",
            args=None,
        )

        # Act
        output = _parse(formatter.format(record))

        # Assert
        assert output["msg"] == "Application startup complete"
        assert output["level"] == "INFO"
        assert "time" in output
        assert "caller" in output

    def test_ignores_color_message(self, formatter: UvicornFormatter) -> None:
        """Test that uvicorn's color_message attribute is excluded."""
        # Arrange
        record = _make_record(
            name="uvicorn.error",
            msg="Started server",
            args=None,
        )
        record.__dict__["color_message"] = "\x1b[32mStarted server\x1b[0m"

        # Act
        output = _parse(formatter.format(record))

        # Assert
        assert "color_message" not in output
        assert output["msg"] == "Started server"

    def test_ignores_asctime(self, formatter: UvicornFormatter) -> None:
        """Test that asctime attribute is excluded (we use our own time)."""
        # Arrange
        record = _make_record(
            name="uvicorn.error",
            msg="test",
            args=None,
        )
        record.__dict__["asctime"] = "2026-01-01 00:00:00"

        # Act
        output = _parse(formatter.format(record))

        # Assert
        assert "asctime" not in output


class TestUvicornAccessFormatter:
    """Test UvicornAccessFormatter for access log messages."""

    def test_access_log_structured_fields(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log extracts structured fields from uvicorn args."""
        # Arrange
        record = _make_record()

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["client_addr"] == "127.0.0.1:8000"
        assert output["method"] == "GET"
        assert output["full_path"] == "/api/v1/users"
        assert output["http_version"] == "1.1"
        assert output["status_code"] == HTTPStatus.OK

    def test_access_log_short_msg(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log msg is short summary instead of verbose string."""
        # Arrange
        record = _make_record()

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["msg"] == "GET /api/v1/users 200"

    def test_access_log_with_query_string(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log handles paths with query strings."""
        # Arrange
        record = _make_record(
            args=(
                "10.0.0.1:12345",
                "GET",
                "/api/v1/meals?from=2026-03-30&to=2026-04-05",
                "1.1",
                HTTPStatus.OK,
            ),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert (
            output["full_path"] == "/api/v1/meals?from=2026-03-30&to=2026-04-05"
        )
        assert (
            output["msg"]
            == "GET /api/v1/meals?from=2026-03-30&to=2026-04-05 200"
        )

    def test_access_log_various_status_codes(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log handles different HTTP status codes."""
        # Arrange
        record = _make_record(
            args=(
                "127.0.0.1:8000",
                "POST",
                "/api/v1/users",
                "1.1",
                HTTPStatus.CREATED,
            ),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["method"] == "POST"
        assert output["status_code"] == HTTPStatus.CREATED
        assert output["msg"] == "POST /api/v1/users 201"

    def test_access_log_error_status(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log handles error status codes."""
        # Arrange
        record = _make_record(
            args=(
                "127.0.0.1:8000",
                "DELETE",
                "/api/v1/users/42",
                "1.1",
                HTTPStatus.NOT_FOUND,
            ),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["status_code"] == HTTPStatus.NOT_FOUND
        assert output["msg"] == "DELETE /api/v1/users/42 404"

    def test_access_log_http2(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log handles HTTP/2."""
        # Arrange
        record = _make_record(
            args=(
                "127.0.0.1:8000",
                "GET",
                "/api/v1/health",
                "2",
                HTTPStatus.OK,
            ),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["http_version"] == "2"

    def test_fallback_when_args_not_tuple(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test formatter handles non-tuple args gracefully (non-access log)."""
        # Arrange
        record = _make_record(
            name="uvicorn.error",
            msg="Some error message",
            args=None,
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["msg"] == "Some error message"
        assert "client_addr" not in output
        assert "method" not in output

    def test_fallback_when_args_too_short(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test formatter handles tuple args with fewer than 5 elements."""
        # Arrange
        record = _make_record(
            msg="%s %s",
            args=("foo", "bar"),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["msg"] == "foo bar"
        assert "client_addr" not in output

    def test_fallback_when_args_is_dict(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test formatter handles dict args (%-style named formatting)."""
        # Arrange
        record = _make_record(
            msg="%(key)s happened",
            args=None,
        )
        # Simulate dict-style args (bypass LogRecord.__init__ validation)
        record.args = {"key": "something"}

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["msg"] == "something happened"

    def test_access_log_with_extra_args(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test formatter handles tuples with more than 5 elements."""
        # Arrange
        record = _make_record(
            args=(
                "127.0.0.1:8000",
                "GET",
                "/api/v1/health",
                "1.1",
                HTTPStatus.OK,
                "extra_field",
            ),
        )

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert output["client_addr"] == "127.0.0.1:8000"
        assert output["status_code"] == HTTPStatus.OK
        assert output["msg"] == "GET /api/v1/health 200"

    def test_core_fields_present(
        self, access_formatter: UvicornAccessFormatter
    ) -> None:
        """Test access log always includes time, level, and caller."""
        # Arrange
        record = _make_record()

        # Act
        output = _parse(access_formatter.format(record))

        # Assert
        assert "time" in output
        assert output["level"] == "INFO"
        assert "caller" in output

    def test_integration_with_logging_config(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test formatters work when used via logging.config.dictConfig."""
        # Arrange
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")

        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "default": {
                        "()": "grelmicro.logging.uvicorn.UvicornFormatter",
                    },
                    "access": {
                        "()": "grelmicro.logging.uvicorn.UvicornAccessFormatter",
                    },
                },
                "handlers": {
                    "default": {
                        "class": "logging.StreamHandler",
                        "formatter": "default",
                        "stream": "ext://sys.stdout",
                    },
                    "access": {
                        "class": "logging.StreamHandler",
                        "formatter": "access",
                        "stream": "ext://sys.stdout",
                    },
                },
                "loggers": {
                    "uvicorn.test": {
                        "handlers": ["default"],
                        "level": "INFO",
                        "propagate": False,
                    },
                    "uvicorn.test.access": {
                        "handlers": ["access"],
                        "level": "INFO",
                        "propagate": False,
                    },
                },
            }
        )

        # Act
        access_logger = logging.getLogger("uvicorn.test.access")
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "PUT",
            "/api/v1/items/1",
            "1.1",
            HTTPStatus.NO_CONTENT,
        )

        error_logger = logging.getLogger("uvicorn.test")
        error_logger.info("Application startup complete")

        # Assert
        _expected_lines = 2
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == _expected_lines

        access_record = _parse(lines[0])
        assert access_record["msg"] == "PUT /api/v1/items/1 204"
        assert access_record["method"] == "PUT"
        assert access_record["status_code"] == HTTPStatus.NO_CONTENT

        error_record = _parse(lines[1])
        assert error_record["msg"] == "Application startup complete"


class TestUvicornFormatterFormats:
    """Test UvicornFormatter respects LOG_FORMAT for all formats."""

    @pytest.mark.parametrize("fmt", ["logfmt", "text", "pretty"])
    def test_uvicorn_formatter_non_json(
        self,
        fmt: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test UvicornFormatter produces correct output for non-JSON formats."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", fmt)
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")
        formatter = UvicornFormatter()
        record = _make_record(
            name="uvicorn.error",
            msg="Application startup complete",
            args=None,
        )

        # Act
        output = formatter.format(record)

        # Assert
        assert "Application startup complete" in output
        assert "INFO" in output

    def test_uvicorn_formatter_logfmt_structure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test UvicornFormatter logfmt has key=value structure."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "logfmt")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        formatter = UvicornFormatter()
        record = _make_record(
            name="uvicorn.error",
            msg="Started server",
            args=None,
        )

        # Act
        output = formatter.format(record)

        # Assert
        assert "level=INFO" in output
        assert 'msg="Started server"' in output
        assert "time=" in output

    def test_uvicorn_formatter_text_single_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test UvicornFormatter TEXT produces single line."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "text")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")
        formatter = UvicornFormatter()
        record = _make_record(
            name="uvicorn.error",
            msg="Started server",
            args=None,
        )

        # Act
        output = formatter.format(record)

        # Assert
        assert output.count("\n") == 0
        assert " - Started server" in output

    def test_uvicorn_formatter_pretty_multi_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test UvicornFormatter PRETTY produces multi-line."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "pretty")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        monkeypatch.setenv("NO_COLOR", "1")
        formatter = UvicornFormatter()
        record = _make_record(
            name="uvicorn.error",
            msg="Started server",
            args=None,
        )

        # Act
        output = formatter.format(record)

        # Assert
        lines = output.splitlines()
        assert len(lines) >= 2  # noqa: PLR2004
        assert "Started server" in lines[0]
        assert "at " in lines[1]

    def test_uvicorn_access_formatter_logfmt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test UvicornAccessFormatter logfmt includes access fields."""
        # Arrange
        monkeypatch.setenv("LOG_FORMAT", "logfmt")
        monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
        formatter = UvicornAccessFormatter()
        record = _make_record()

        # Act
        output = formatter.format(record)

        # Assert
        assert "method=GET" in output
        assert "full_path=/api/v1/users" in output
        assert "status_code=200" in output


_UVICORN_APP = """\
async def app(scope, receive, send):
    if scope["type"] == "http":
        await receive()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/plain"]],
        })
        await send({"type": "http.response.body", "body": b"ok"})
"""

_STARTUP_TIMEOUT = 10
_POLL_INTERVAL = 0.1


class TestUvicornProcess:
    """Integration tests that start a real uvicorn process."""

    @pytest.mark.integration
    @pytest.mark.timeout(15)
    def test_real_uvicorn_access_log(self) -> None:
        """Test structured JSON output from a real uvicorn process."""
        # Arrange: write temp app and log config
        with tempfile.TemporaryDirectory() as tmpdir:
            app_file = Path(tmpdir) / "app.py"
            app_file.write_text(_UVICORN_APP)

            config_file = Path(tmpdir) / "log_config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "disable_existing_loggers": False,
                        "formatters": {
                            "default": {
                                "()": "grelmicro.logging.uvicorn.UvicornFormatter",
                            },
                            "access": {
                                "()": "grelmicro.logging.uvicorn.UvicornAccessFormatter",
                            },
                        },
                        "handlers": {
                            "default": {
                                "class": "logging.StreamHandler",
                                "formatter": "default",
                                "stream": "ext://sys.stdout",
                            },
                            "access": {
                                "class": "logging.StreamHandler",
                                "formatter": "access",
                                "stream": "ext://sys.stdout",
                            },
                        },
                        "loggers": {
                            "uvicorn": {
                                "handlers": ["default"],
                                "level": "INFO",
                                "propagate": False,
                            },
                            "uvicorn.access": {
                                "handlers": ["access"],
                                "level": "INFO",
                                "propagate": False,
                            },
                        },
                    }
                )
            )

            # Act: start uvicorn
            proc = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9876",
                    "--log-config",
                    str(config_file),
                ],
                cwd=tmpdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={
                    **__import__("os").environ,
                    "LOG_OTEL_ENABLED": "false",
                },
            )

            try:
                # Wait for uvicorn to start
                started = False
                deadline = time.monotonic() + _STARTUP_TIMEOUT
                while time.monotonic() < deadline:
                    try:
                        urllib.request.urlopen("http://127.0.0.1:9876/")
                        started = True
                        break
                    except (ConnectionError, OSError):
                        time.sleep(_POLL_INTERVAL)

                assert started, "Uvicorn did not start in time"

                # Send a request
                resp = urllib.request.urlopen(
                    "http://127.0.0.1:9876/api/v1/health"
                )
                assert resp.status == HTTPStatus.OK
            finally:
                proc.terminate()
                proc.wait(timeout=5)

            # Assert: parse stdout
            assert proc.stdout is not None
            raw = proc.stdout.read().decode()
            lines = raw.strip().splitlines()

            json_lines = []
            for line in lines:
                try:
                    json_lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            # Find the access log for our /api/v1/health request
            access_logs = [
                r for r in json_lines if r.get("full_path") == "/api/v1/health"
            ]
            assert len(access_logs) >= 1, (
                f"No access log for /api/v1/health found in: {json_lines}"
            )

            record = access_logs[0]
            assert record["method"] == "GET"
            assert record["status_code"] == HTTPStatus.OK
            assert record["msg"] == "GET /api/v1/health 200"
            assert record["level"] == "INFO"
            assert "time" in record
            assert "caller" in record
            assert "client_addr" in record
            assert "http_version" in record

            # Find a startup log (from UvicornFormatter)
            startup_logs = [
                r
                for r in json_lines
                if "Started server" in r.get("msg", "")
                or "started" in r.get("msg", "").lower()
            ]
            assert len(startup_logs) >= 1, (
                f"No startup log found in: {json_lines}"
            )
            assert "caller" in startup_logs[0]
            assert "time" in startup_logs[0]
