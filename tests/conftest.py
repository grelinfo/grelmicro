"""grelmicro Test Config."""

from collections.abc import AsyncIterator

import pytest

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
