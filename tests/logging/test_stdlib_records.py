"""A record written through stdlib logging reaches the configured format.

Every grelmicro component logs through `logging.getLogger("grelmicro.*")`,
and so does every dependency a service runs: httpx, SQLAlchemy, redis. They
have to render the way the app configured, whichever backend the app writes
its own records through.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import pytest
from uvicorn.config import LOGGING_CONFIG

from grelmicro.log import configure_with
from grelmicro.log.config import LogConfig, LogFormatType
from tests.logging.conftest import BACKENDS, parse_json_log

if TYPE_CHECKING:
    from collections.abc import Iterator

COMPONENT_LOGGER = "grelmicro.health"


@pytest.fixture
def uvicorn_loggers() -> Iterator[io.StringIO]:
    """Give the uvicorn loggers what uvicorn's own config gives them.

    Read from `uvicorn.config.LOGGING_CONFIG`, so the propagation this
    rests on is uvicorn's rather than a guess, and built by hand rather
    than through `dictConfig`, which reconfigures logging process-wide
    and would tear down the handler `caplog` installs for every later
    test.

    The handler writes to a buffer of its own rather than to whatever
    stream it finds, so what uvicorn wrote is read from the buffer and
    what the root handler wrote from the captured stream, with no
    ordering between fixtures to get right.
    """
    names = ("uvicorn", "uvicorn.access")
    before = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in names
    }
    sink = io.StringIO()
    for name in names:
        logger = logging.getLogger(name)
        logger.handlers = [logging.StreamHandler(sink)]
        logger.propagate = LOGGING_CONFIG["loggers"][name]["propagate"]
        logger.setLevel(logging.INFO)
    try:
        yield sink
    finally:
        for name, (handlers, propagate, level) in before.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.usefixtures("reset_backend")
def test_a_stdlib_record_renders_in_the_configured_format(
    backend: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is a full record, not the bare message `lastResort` writes."""
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(
        LogConfig(backend=backend, format=LogFormatType.JSON, level="INFO")
    )

    logging.getLogger(COMPONENT_LOGGER).info(
        "a component record", extra={"check": "db"}
    )

    record = parse_json_log(capsys.readouterr().out)
    assert record["msg"] == "a component record"
    assert record["logger"] == COMPONENT_LOGGER
    assert record["level"] == "INFO"
    assert record["check"] == "db"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.usefixtures("reset_backend")
def test_the_root_level_follows_the_configured_level(
    backend: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A debug record is written when debug was asked for.

    The root logger defaults to `WARNING`, so a backend that installs no
    handler drops every info and debug record without a word.
    """
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(
        LogConfig(backend=backend, format=LogFormatType.JSON, level="DEBUG")
    )

    logging.getLogger(COMPONENT_LOGGER).debug("a quiet record")

    assert parse_json_log(capsys.readouterr().out)["level"] == "DEBUG"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.usefixtures("reset_backend")
def test_an_exception_is_rendered_as_the_error_field(
    backend: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`exc_info` reads the same as it does on the stdlib backend."""
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(
        LogConfig(backend=backend, format=LogFormatType.JSON, level="INFO")
    )

    try:
        msg = "the backend refused"
        raise ValueError(msg)  # noqa: TRY301
    except ValueError:
        logging.getLogger(COMPONENT_LOGGER).exception("a check failed")

    record = parse_json_log(capsys.readouterr().out)
    assert record["error"]["type"] == "ValueError"
    assert record["error"]["message"] == "the backend refused"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.usefixtures("reset_backend")
def test_a_uvicorn_record_is_written_once(
    backend: str,
    uvicorn_loggers: io.StringIO,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request line stays one line.

    What keeps it to one is uvicorn's own config: its access logger
    carries a handler and does not propagate, so the root handler this
    change installs never sees the record. The guard is against grelmicro
    routing those records to the root as well, by turning propagation on
    or by adding an interceptor of its own.

    A service that hands uvicorn a `log_config` leaving propagation on is
    outside this, and reads every request line twice, which is what
    `install_root` documents.
    """
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    access = logging.getLogger("uvicorn.access")
    configure_with(
        LogConfig(backend=backend, format=LogFormatType.JSON, level="INFO")
    )

    access.info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 200
    )

    # Uvicorn's own handler and the root handler both, so a duplicate
    # cannot hide on the one an assertion about the other never reads.
    captured = capsys.readouterr()
    written = [
        line
        for line in (
            uvicorn_loggers.getvalue() + captured.out + captured.err
        ).splitlines()
        if line.strip() and "Logging error" not in line
    ]
    assert len(written) == 1


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        pytest.param("{extra[serialized]}", "json", id="json-template"),
        pytest.param(
            "{extra[logfmt_serialized]}", "logfmt", id="logfmt-template"
        ),
    ],
)
def test_a_loguru_template_renders_both_sides_the_same(
    template: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """A template asking for the serialized record is read as its format.

    `LogConfig.format` takes a loguru template, and a `logging.Formatter`
    cannot read one. What the two known templates render is known all the
    same, so both sides of the process write it rather than the app's
    records landing in one format and its dependencies' in another.
    """
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(LogConfig(backend="loguru", format=template, level="INFO"))

    logging.getLogger(COMPONENT_LOGGER).info("a component record")

    written = capsys.readouterr().out.strip()
    if expected == "json":
        assert parse_json_log(written)["msg"] == "a component record"
    else:
        assert 'msg="a component record"' in written


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("template", "starts_with"),
    [
        pytest.param("{extra[serialized]}", "{", id="json-template"),
        pytest.param("{extra[logfmt_serialized]}", "time=", id="logfmt"),
    ],
)
def test_a_template_reads_the_same_on_every_backend(
    backend: str,
    template: str,
    starts_with: str,
    uvicorn_loggers: io.StringIO,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """The rule lives in one place, so no backend can forget to ask.

    A template is loguru's, and the loguru backend is not the only writer
    that has to know what it renders: the root handler and the uvicorn
    formatter render the same process.
    """
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(LogConfig(backend=backend, format=template, level="INFO"))

    logging.getLogger(COMPONENT_LOGGER).info("a dependency record")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 200
    )

    assert capsys.readouterr().out.strip().startswith(starts_with)
    assert uvicorn_loggers.getvalue().strip().startswith(starts_with)


def test_a_loguru_template_reaches_uvicorns_records_too(
    uvicorn_loggers: io.StringIO,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reset_backend: None,  # noqa: ARG001
) -> None:
    """Three writers, one format.

    The app writes through loguru, its dependencies through the standard
    library, and uvicorn through handlers of its own. A template that is
    one of our formats has to reach all three, or the process emits two
    shapes and the reader has to know which line came from where.
    """
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    configure_with(
        LogConfig(
            backend="loguru", format="{extra[logfmt_serialized]}", level="INFO"
        )
    )

    logging.getLogger(COMPONENT_LOGGER).info("a dependency record")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 200
    )

    dependency = capsys.readouterr().out.strip()
    uvicorn_line = uvicorn_loggers.getvalue().strip()

    assert dependency.startswith("time=")
    assert uvicorn_line.startswith("time=")
    assert "logger=uvicorn.access" in uvicorn_line
