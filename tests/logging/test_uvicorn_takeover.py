"""Tests for reformatting uvicorn's own loggers to match the app format."""

import io
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from grelmicro.log._apply import apply as apply_backend
from grelmicro.log._queue import get_stream
from grelmicro.log.config import LogBackendType, LogConfig, LogFormatType
from grelmicro.log.uvicorn import UvicornAccessFormatter, UvicornFormatter
from grelmicro.log.uvicorn import apply as apply_uvicorn

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_LOGFMT = LogConfig(format=LogFormatType.LOGFMT)


@pytest.fixture(autouse=True)
def _uvicorn_logging() -> Iterator[None]:
    """Give the uvicorn loggers handlers, as uvicorn's own config does.

    Built directly rather than through `logging.config.dictConfig`, which
    reconfigures logging process-wide and would tear down the handler
    `caplog` installs for every later test. The root logger is never
    touched, so these tests leave no state behind.
    """
    before = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
        )
        for name in _UVICORN_LOGGERS
    }
    for name in ("uvicorn", "uvicorn.access"):
        logger = logging.getLogger(name)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers = [handler]
        logger.propagate = False

    yield

    for name, (handlers, propagate) in before.items():
        logger = logging.getLogger(name)
        logger.handlers = handlers
        logger.propagate = propagate


def _formatters(name: str) -> list[logging.Formatter | None]:
    return [handler.formatter for handler in logging.getLogger(name).handlers]


def _access_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "POST", "/orders", "1.1", 200),
        exc_info=None,
    )


def test_reformats_uvicorn_handlers() -> None:
    """The formatter uvicorn installed is replaced with a matching one."""
    apply_uvicorn(_LOGFMT)

    assert all(
        isinstance(formatter, UvicornFormatter)
        for formatter in _formatters("uvicorn")
    )
    assert all(
        isinstance(formatter, UvicornAccessFormatter)
        for formatter in _formatters("uvicorn.access")
    )


def test_handlers_are_kept_not_replaced() -> None:
    """The handler objects survive, so a custom one is never dropped."""
    before = {
        name: list(logging.getLogger(name).handlers)
        for name in _UVICORN_LOGGERS
    }

    apply_uvicorn(_LOGFMT)

    for name, handlers in before.items():
        assert logging.getLogger(name).handlers == handlers


def test_access_record_keeps_structured_fields() -> None:
    """The access formatter still splits the request into fields."""
    apply_uvicorn(_LOGFMT)
    formatter = _formatters("uvicorn.access")[0]
    assert formatter is not None

    line = formatter.format(_access_record())

    assert "client_addr=127.0.0.1:54321" in line
    assert "method=POST" in line
    assert "status_code=200" in line


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_the_stream_follows_the_one_the_process_writes_to(stream: str) -> None:
    """Uvicorn's own stream would go around the queue and split the output."""
    handler = logging.StreamHandler(getattr(sys, stream))
    logging.getLogger("uvicorn").handlers = [handler]

    apply_uvicorn(_LOGFMT)

    assert handler.stream is get_stream()


def test_a_handler_pointed_somewhere_else_keeps_its_stream() -> None:
    """A stream that was chosen is not one to take over."""
    chosen = io.StringIO()
    handler = logging.StreamHandler(chosen)
    logger = logging.getLogger("uvicorn")
    logger.handlers = [handler]

    apply_uvicorn(_LOGFMT)

    assert handler.stream is chosen


def test_a_file_handler_keeps_the_file_it_opened(tmp_path: Path) -> None:
    """A `FileHandler` is a `StreamHandler`, and moving it abandons the file."""
    path = tmp_path / "uvicorn.log"
    handler = logging.FileHandler(path)
    logger = logging.getLogger("uvicorn")
    logger.handlers = [handler]

    try:
        apply_uvicorn(_LOGFMT)
        logger.info("to the file")
    finally:
        handler.close()

    assert "to the file" in path.read_text()


def test_resolved_config_is_used_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A format passed as a keyword argument never reaches the environment.

    The formatters read settings themselves, so they have to be handed the
    resolved config rather than re-reading `GREL_LOG_FORMAT`.
    """
    monkeypatch.setenv("GREL_LOG_FORMAT", "JSON")
    apply_uvicorn(_LOGFMT)
    formatter = _formatters("uvicorn")[0]
    assert formatter is not None
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=None,
        exc_info=None,
    )

    assert formatter.format(record).startswith("time=")


def test_apply_backend_respects_the_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`uvicorn_enabled=False` is for an app configuring uvicorn elsewhere."""
    calls: list[LogConfig] = []

    def _record_call(config: LogConfig) -> None:
        calls.append(config)

    def _noop(config: LogConfig) -> None:
        _ = config

    monkeypatch.setattr("grelmicro.log.uvicorn.apply", _record_call)
    monkeypatch.setattr("grelmicro.log._stdlib.configure", _noop)

    apply_backend(LogConfig(uvicorn_enabled=False))
    assert calls == []

    apply_backend(LogConfig(uvicorn_enabled=True))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "backend",
    [LogBackendType.STDLIB, LogBackendType.STRUCTLOG, LogBackendType.LOGURU],
)
@pytest.mark.usefixtures("reset_backend")
def test_uvicorn_matches_the_app_format_on_every_backend(
    backend: LogBackendType,
) -> None:
    """Uvicorn's own records are rendered by grelmicro, whatever runs the app.

    The app's records go through the selected backend, and uvicorn keeps its
    own stdlib handlers. Both ends render through the same writers, so one
    process emits one format.
    """
    apply_backend(LogConfig(backend=backend, format=LogFormatType.LOGFMT))
    formatter = _formatters("uvicorn.access")[0]
    assert formatter is not None

    line = formatter.format(_access_record())

    assert "method=POST" in line
    assert "status_code=200" in line


@pytest.mark.parametrize(
    "backend",
    [LogBackendType.STDLIB, LogBackendType.STRUCTLOG, LogBackendType.LOGURU],
)
@pytest.mark.usefixtures("reset_backend")
def test_the_access_record_survives_every_backend(
    backend: LogBackendType,
) -> None:
    """Formatting leaves the record as it found it, on every backend."""
    apply_backend(LogConfig(backend=backend, format=LogFormatType.LOGFMT))
    formatter = _formatters("uvicorn.access")[0]
    assert formatter is not None
    record = _access_record()

    formatter.format(record)

    assert record.getMessage() == (
        '127.0.0.1:54321 - "POST /orders HTTP/1.1" 200'
    )
