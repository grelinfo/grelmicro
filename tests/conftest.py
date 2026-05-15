"""grelmicro Test Config."""

import pytest

from grelmicro.health._backends import health_checks
from grelmicro.resilience._backends import (
    circuit_breaker_backend_registry,
    rate_limiter_backend_registry,
)


@pytest.fixture(autouse=True)
def _reset_backend_registries() -> None:
    """Reset every remaining internal backend registry before each test.

    The sync and cache registries were removed in #207: those kinds resolve
    through the `Grelmicro` app. The rate limiter, circuit breaker, and
    health registries are still internal infrastructure and benefit from
    the per-test reset to avoid leak-across when test order is randomised.
    """
    rate_limiter_backend_registry.reset()
    circuit_breaker_backend_registry.reset()
    health_checks.reset()


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
