"""An app with no declared tier says so at startup: `undeclared-environment`."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import warnings
from typing import TYPE_CHECKING

import pytest

from grelmicro import Grelmicro
from grelmicro._config import flush_ignored_env_reports
from grelmicro._environment import report_undeclared_environment
from tests._logs import records_of

if TYPE_CHECKING:
    from grelmicro.types import Environment

pytestmark = [pytest.mark.timeout(5)]


def _undeclared_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the messages of the `undeclared-environment` log records."""
    flush_ignored_env_reports()
    return [
        record.getMessage()
        for record in records_of(caplog, "grelmicro")
        if getattr(record, "diagnostic", None) == "undeclared-environment"
    ]


async def test_an_undeclared_environment_logs_one_warning_per_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Opening the app logs that the production checks are off, once."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    micro = Grelmicro()

    # Act
    async with micro:
        pass

    # Assert
    messages = _undeclared_records(caplog)
    assert len(messages) == 1
    assert messages[0].startswith(
        "No environment declared, so the production checks are off. "
        "Set GREL_ENVIRONMENT to development, test, staging or production."
    )
    record = next(
        r
        for r in records_of(caplog, "grelmicro")
        if getattr(r, "diagnostic", None) == "undeclared-environment"
    )
    assert record.levelname == "WARNING"


async def test_an_undeclared_environment_logs_on_every_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With logging configured, each open of the app logs its own line."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    flush_ignored_env_reports()
    micro = Grelmicro()
    opens = 2

    # Act
    for _ in range(opens):
        async with micro:
            pass

    # Assert
    assert len(_undeclared_records(caplog)) == opens


async def test_without_log_the_line_is_logged_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An app with no `Log` logs the line during the open, not later."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    micro = Grelmicro()

    # Act
    async with micro:
        pass

    # Assert
    assert [
        r.levelno
        for r in records_of(caplog, "grelmicro")
        if getattr(r, "diagnostic", None) == "undeclared-environment"
    ] == [logging.WARNING]


def test_without_any_handler_the_line_reaches_stderr() -> None:
    """With no logging set up at all, the last-resort handler prints it."""
    # Arrange
    script = (
        "import asyncio\n"
        "from grelmicro import Grelmicro\n"
        "async def main():\n"
        "    async with Grelmicro():\n"
        "        pass\n"
        "asyncio.run(main())\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GREL_")}

    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=30,
    )

    # Assert
    lines = [
        line
        for line in result.stderr.splitlines()
        if "[undeclared-environment]" in line
    ]
    assert len(lines) == 1
    assert lines[0].startswith("No environment declared")


def test_deferred_reports_queue_one_line_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Opens made before `Log` configures logging wait as one line."""
    # Act
    for _ in range(3):
        report_undeclared_environment(deferred=True)

    queued = records_of(caplog, "grelmicro")

    # Assert
    assert queued == []
    assert len(_undeclared_records(caplog)) == 1


async def test_an_undeclared_environment_is_no_python_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suite run with `-W error` keeps passing."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    micro = Grelmicro()

    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            pass


@pytest.mark.parametrize(
    "environment", ["development", "test", "staging", "production"]
)
async def test_a_declared_environment_logs_nothing(
    environment: Environment, caplog: pytest.LogCaptureFixture
) -> None:
    """Any declared tier, `development` and `test` included, stays quiet."""
    # Arrange
    micro = Grelmicro(environment=environment)

    # Act
    async with micro:
        pass

    # Assert
    assert _undeclared_records(caplog) == []


async def test_a_declared_environment_variable_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`GREL_ENVIRONMENT=test`, set by the suite, declares the tier."""
    # Arrange
    micro = Grelmicro()

    # Act
    async with micro:
        pass

    # Assert
    assert _undeclared_records(caplog) == []


def test_describe_renders_an_undeclared_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report keeps `None` and renders it as `undeclared`."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    micro = Grelmicro()

    # Act
    report = micro.describe()

    # Assert
    assert report.environment is None
    assert report.render().startswith("Environment: undeclared\n")
