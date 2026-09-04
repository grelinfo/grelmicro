"""A record written through stdlib logging reaches the configured format.

Every grelmicro component logs through `logging.getLogger("grelmicro.*")`,
and so does every dependency a service runs: httpx, SQLAlchemy, redis. They
have to render the way the app configured, whichever backend the app writes
its own records through.
"""

from __future__ import annotations

import logging

import pytest

from grelmicro.log import configure_with
from grelmicro.log.config import LogConfig, LogFormatType
from tests.logging.conftest import BACKENDS, parse_json_log

COMPONENT_LOGGER = "grelmicro.health"


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
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn keeps its own handlers, so the root must not answer too.

    Uvicorn installs a handler on its access logger and turns propagation
    off, and grelmicro reformats that handler in place. A root handler that
    also caught the record would write every request line twice.
    """
    monkeypatch.setenv("GREL_LOG_BACKEND", backend)
    monkeypatch.setenv("GREL_LOG_OTEL_ENABLED", "false")
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.StreamHandler()]
    access.propagate = False
    access.setLevel(logging.INFO)
    try:
        configure_with(
            LogConfig(backend=backend, format=LogFormatType.JSON, level="INFO")
        )

        access.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 200
        )
    finally:
        access.handlers = []
        access.propagate = True

    # Both streams: uvicorn's handler writes to stderr and the root
    # handler to stdout, so a duplicate lands on the stream an assertion
    # about one of them would never read.
    captured = capsys.readouterr()
    written = [
        line
        for line in (captured.out + captured.err).splitlines()
        if line.strip()
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
