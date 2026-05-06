"""grelmicro Test Config."""

import pytest

from grelmicro.cache._backends import cache_backend_registry
from grelmicro.resilience._backends import (
    circuit_breaker_backend_registry,
    rate_limiter_backend_registry,
)
from grelmicro.sync._backends import sync_backend_registry


@pytest.fixture(autouse=True)
def _reset_backend_registries() -> None:
    """Reset every backend registry before each test.

    Tests register backends ad-hoc and the order they run in is
    randomised, so a leaked entry from one test can fail an
    unrelated one with ``BackendAlreadyRegisteredError``. Resetting
    up-front isolates each test from the rest.
    """
    sync_backend_registry.reset()
    cache_backend_registry.reset()
    rate_limiter_backend_registry.reset()
    circuit_breaker_backend_registry.reset()


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
