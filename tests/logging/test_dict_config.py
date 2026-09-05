"""Tests for the logging configuration an application server consumes."""

import json
import logging
import logging.config
import pickle
from collections.abc import Iterator
from typing import Any

import pytest

from grelmicro.errors import DependencyNotFoundError
from grelmicro.log import dict_config, dict_config_with, formatter
from grelmicro.log.config import LogConfig, LogFormatType, LogLevelType

_UVICORN_ACCESS = "uvicorn.access"
_SERVERS = (
    "uvicorn",
    "uvicorn.error",
    "gunicorn.error",
    "gunicorn.access",
    "hypercorn.error",
    "hypercorn.access",
    "_granian",
    "granian.access",
)
_ACCESS_MESSAGE = '%s - "%s %s HTTP/%s" %d'
_ACCESS_ARGS = ("127.0.0.1:54321", "GET", "/orders", "1.1", 200)
_OK = 200


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Put logging back after a document has been applied.

    `dictConfig` reconfigures logging process-wide, so the root logger and
    every logger the document names are restored. Otherwise the handler
    `caplog` installs is gone for every later test.
    """
    root = logging.getLogger()
    root_before = (list(root.handlers), root.level)
    before = [
        (
            name,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in (*_SERVERS, _UVICORN_ACCESS)
    ]

    yield

    root.handlers, root.level = root_before
    for name, handlers, propagate, level in before:
        logger = logging.getLogger(name)
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(level)


def _apply(document: dict[str, Any]) -> None:
    """Apply the document the way an application server does."""
    logging.config.dictConfig(document)


@pytest.mark.usefixtures("_restore_logging")
def test_every_server_logger_reaches_the_root_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A line from any application server renders like an application line."""
    _apply(dict_config_with(LogConfig(format=LogFormatType.LOGFMT)))

    for name in _SERVERS:
        logging.getLogger(name).info("server line")
    logging.getLogger("myapp").info("app line")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == len(_SERVERS) + 1
    for line, name in zip(lines, (*_SERVERS, "myapp"), strict=True):
        assert line.startswith("time=")
        assert f"logger={name}" in line


@pytest.mark.usefixtures("_restore_logging")
def test_the_uvicorn_access_record_keeps_its_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Uvicorn carries the request in arguments, so it gets its own formatter."""
    _apply(dict_config_with(LogConfig(format=LogFormatType.JSON)))

    logging.getLogger(_UVICORN_ACCESS).info(_ACCESS_MESSAGE, *_ACCESS_ARGS)

    record = json.loads(capsys.readouterr().out.strip())
    assert record["msg"] == "GET /orders 200"
    assert record["method"] == "GET"
    assert record["full_path"] == "/orders"
    assert record["status_code"] == _OK
    assert record["client_addr"] == "127.0.0.1:54321"


@pytest.mark.usefixtures("_restore_logging")
def test_the_ansi_copy_uvicorn_attaches_is_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Uvicorn logs `color_message`, which renders as escapes in a field."""
    _apply(dict_config_with(LogConfig(format=LogFormatType.JSON)))

    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]",
        7,
        extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
    )

    record = json.loads(capsys.readouterr().out.strip())
    assert record["msg"] == "Started server process [7]"
    assert "color_message" not in record


def test_the_document_is_json_and_a_fresh_copy() -> None:
    """It is written to the file `uvicorn --log-config` reads."""
    document = dict_config()

    assert json.loads(json.dumps(document)) == document
    assert dict_config() is not document
    assert dict_config()["formatters"] is not document["formatters"]


def test_the_pre_built_document_survives_a_worker_spawn() -> None:
    """`--workers` pickles the document to every child."""
    document = dict_config_with(LogConfig(level=LogLevelType.DEBUG))

    assert pickle.loads(pickle.dumps(document)) == document  # noqa: S301
    assert document["root"] == {"handlers": ["default"], "level": "DEBUG"}


def test_the_two_formatter_names_uvicorn_writes_into_are_present() -> None:
    """`--use-colors` and `--no-use-colors` raise when either is missing."""
    document = dict_config()

    assert set(document["formatters"]) == {"default", "access"}


@pytest.mark.parametrize("use_colors", [True, False])
@pytest.mark.usefixtures("_restore_logging")
def test_a_server_may_turn_colors_on_or_off(
    capsys: pytest.CaptureFixture[str],
    *,
    use_colors: bool,
) -> None:
    """Uvicorn writes `use_colors` into both formatters before applying."""
    document = dict_config_with(LogConfig(format=LogFormatType.TEXT))
    for options in document["formatters"].values():  # type: ignore[union-attr]
        options["use_colors"] = use_colors
    _apply(document)

    logging.getLogger("uvicorn.error").info("startup")

    assert ("\x1b[" in capsys.readouterr().out) is use_colors


@pytest.mark.usefixtures("_restore_logging")
def test_colors_never_reach_a_machine_read_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A JSON field holding an escape sequence would not parse."""
    document = dict_config_with(LogConfig(format=LogFormatType.JSON))
    for options in document["formatters"].values():  # type: ignore[union-attr]
        options["use_colors"] = True
    _apply(document)

    logging.getLogger("uvicorn.error").info("startup")

    assert json.loads(capsys.readouterr().out.strip())["msg"] == "startup"


def test_an_invalid_configuration_is_refused_when_it_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document raises where it is written, not inside the server."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOG_JSON_SERIALIZER", "orjson")
    monkeypatch.setattr("grelmicro.log._shared.has_orjson", lambda: False)

    with pytest.raises(DependencyNotFoundError):
        dict_config()


def test_the_formatter_reads_the_environment_when_no_config_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document a server reads from a file carries no settings of its own."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOG_FORMAT", "logfmt")
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")

    record = logging.LogRecord(
        name="myapp",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    assert formatter().format(record).startswith("time=")
