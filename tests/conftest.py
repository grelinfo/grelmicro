"""grelmicro Test Config."""

from collections.abc import AsyncIterator

import pytest

from grelmicro import _config, _environment
from grelmicro.clock import VirtualClock


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
