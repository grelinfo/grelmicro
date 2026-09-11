"""grelmicro Test Config."""

import logging
from collections.abc import AsyncIterator, Generator

import pytest
import structlog
from loguru import logger as loguru_logger

from grelmicro import _config, _environment
from grelmicro.clock import VirtualClock
from grelmicro.log import _queue as log_queue


@pytest.fixture
async def clock() -> AsyncIterator[VirtualClock]:
    """Install a `VirtualClock` for the test and yield it.

    Time-dependent primitives read `grelmicro.clock.monotonic` and `sleep`
    through the clock seam, so under this fixture they advance only when the
    test calls `clock.advance(...)`, with no real waiting. Use it instead of
    `async with VirtualClock() as clock:`.
    """
    async with VirtualClock() as virtual_clock:
        yield virtual_clock


@pytest.fixture(autouse=True)
def _opt_in_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the Environmental config path for all tests by default.

    Production code requires ``GREL_ENV_LOAD=true`` to read
    env-driven config. The test suite was written before that opt-in
    existed and assumes env reads run by default. This fixture
    preserves that assumption. Tests that exercise the OFF behavior
    delete the var explicitly with
    ``monkeypatch.delenv("GREL_ENV_LOAD", raising=False)``.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "true")


@pytest.fixture(autouse=True)
def _declare_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the tier so the backend scope check stays quiet by default.

    The suite wires memory backends everywhere, which an undeclared
    environment reports once per app, and `filterwarnings = ["error"]` turns
    every report into a failure. Declaring `test` is what the docs ask of a
    test suite, so the suite does it. Tests that exercise the check set
    `GREL_ENVIRONMENT` themselves or pass `environment=`.
    """
    monkeypatch.setenv("GREL_ENVIRONMENT", "test")


@pytest.fixture(autouse=True)
def _no_leaked_log_backend() -> Generator[None, None, None]:
    """Take out a log sink bound to another test's stream.

    A backend writes to the stream it was handed when it was configured,
    and under `capsys` that stream belongs to the test that configured it.
    Any test that calls `configure()` leaves one behind, and the next test
    to assert on `capsys` reads nothing back, because the record went to a
    stream pytest stopped capturing when the other test ended.

    `tests/logging` asks for `reset_backend` and is safe. Every other
    directory configures logging without it, which is most of the suite,
    so the guard belongs here rather than in each of them.
    """
    loguru_logger.configure(handlers=[])
    structlog.reset_defaults()
    root = logging.getLogger()
    handlers = root.handlers.copy()
    root.handlers.clear()

    yield

    loguru_logger.remove()
    structlog.reset_defaults()
    root.handlers.clear()
    root.handlers.extend(handlers)


@pytest.fixture(autouse=True)
def _no_leaked_log_queue() -> Generator[None, None, None]:
    """Take out a log queue a test left installed.

    `configure(queue_enabled=True)` starts a writer that lives until the
    process ends, and a snippet or a component test can leave one behind.
    The next test to configure logging would bind the sink to that writer,
    which holds the stream of the test that started it, and read nothing
    back from `capsys`.

    Taking one out on the way in as well as on the way out is what makes
    that impossible rather than unlikely. A writer installed at import
    time, or left by a teardown that did not run, reaches the next test
    otherwise, and `install_if_absent` keeps it rather than replacing it.
    """
    log_queue.uninstall()

    yield

    log_queue.uninstall()


@pytest.fixture(autouse=True)
def _reset_ignored_env_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the process-wide ignored-variable report state.

    A report is deduplicated for the life of the process and queued until
    logging is configured. Without a reset, a report made in one test would
    be missing from, or surface in, another.
    """
    _config._warned_ignored_env.clear()
    _config._pending_ignored_env.clear()
    _config._pending_reports.clear()
    _environment._reported_unknown.clear()
    monkeypatch.setattr(_config, "_logging_configured", False)
