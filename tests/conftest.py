"""grelmicro Test Config."""

import pytest


@pytest.fixture(autouse=True)
def _opt_in_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the Environmental config path for all tests by default.

    Production code requires ``GREL_CONFIG_FROM_ENV=true`` to read
    env-driven config. The test suite was written before that opt-in
    existed and assumes env reads run by default. This fixture
    preserves that assumption. Tests that exercise the OFF behavior
    delete the var explicitly with
    ``monkeypatch.delenv("GREL_CONFIG_FROM_ENV", raising=False)``.
    """
    monkeypatch.setenv("GREL_CONFIG_FROM_ENV", "true")
