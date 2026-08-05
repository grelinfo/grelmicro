"""Tests for the GREL_ENV_LOAD opt-in flag."""

import warnings

import pytest

from grelmicro._config import (
    _warned_ignored_env,
    env_load_default,
    resolve_config,
)
from grelmicro.coordination.lock import Lock, LockConfig
from grelmicro.coordination.memory import MemoryLockAdapter

LEASE_OVERRIDE = 999.0
LEASE_FROM_ENV = 42.0
DEFAULT_LEASE = LockConfig.model_fields["lease_duration"].default


@pytest.fixture
def backend() -> MemoryLockAdapter:
    """Memory backend usable without an event loop."""
    return MemoryLockAdapter()


@pytest.fixture
def _no_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse fixture and turn the global flag off."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)


@pytest.fixture(autouse=True)
def _reset_ignored_env_warnings() -> None:
    """Clear the process-wide dedup set so warnings do not depend on test order."""
    _warned_ignored_env.clear()


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_env_opt_in_truthy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truthy values turn the global flag on."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    assert env_load_default() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "anything"])
def test_env_opt_in_falsy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything else keeps the flag off."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    assert env_load_default() is False


@pytest.mark.usefixtures("_no_env_opt_in")
def test_env_ignored_when_flag_off(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the global flag, env vars are not read, and that is reported."""
    monkeypatch.setenv(
        "GREL_LOCK_CART_LEASE_DURATION", str(int(LEASE_OVERRIDE))
    )
    with pytest.warns(UserWarning, match="GREL_LOCK_CART_LEASE_DURATION"):
        lock = Lock("cart", backend=backend)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_no_env_opt_in")
def test_ignored_env_is_reported_once(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ignored variable is reported once, not on every construction."""
    monkeypatch.setenv(
        "GREL_LOCK_CART_LEASE_DURATION", str(int(LEASE_OVERRIDE))
    )
    with pytest.warns(UserWarning, match="GREL_LOCK_CART_LEASE_DURATION"):
        Lock("cart", backend=backend)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)

    assert caught == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_unrelated_prefixed_env_is_not_reported(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only declared field names are matched, never a prefix sweep.

    Kubernetes injects `{SVCNAME}_SERVICE_HOST` for every Service, so a
    prefix sweep would warn on every pod start.
    """
    monkeypatch.setenv("GREL_LOCK_CART_SERVICE_HOST", "10.0.0.1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)

    assert caught == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_explicit_env_load_false_is_not_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit opt-out is a decision, so it is never reported."""
    monkeypatch.setenv(
        "GREL_LOCK_TEST_LEASE_DURATION", str(int(LEASE_FROM_ENV))
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolve_config(
            LockConfig,
            explicit=None,
            kwargs={},
            env_prefix="GREL_LOCK_TEST_",
            env_load=False,
        )

    assert caught == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_per_call_env_load_true_overrides_flag_off(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=True` reads env even when the global flag is off."""
    monkeypatch.setenv(
        "GREL_LOCK_CART_LEASE_DURATION", str(int(LEASE_OVERRIDE))
    )
    lock = Lock("cart", backend=backend, env_load=True)
    assert lock.config.lease_duration == LEASE_OVERRIDE


def test_per_call_env_load_false_overrides_flag_on(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` ignores env even when the global flag is on."""
    # autouse fixture sets GREL_ENV_LOAD=true
    monkeypatch.setenv(
        "GREL_LOCK_CART_LEASE_DURATION", str(int(LEASE_OVERRIDE))
    )
    lock = Lock("cart", backend=backend, env_load=False)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_no_env_opt_in")
def test_resolve_config_respects_global_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_config(env_load=None)` follows the global flag."""
    monkeypatch.setenv(
        "GREL_LOCK_TEST_LEASE_DURATION", str(int(LEASE_FROM_ENV))
    )
    with pytest.warns(UserWarning, match="GREL_LOCK_TEST_LEASE_DURATION"):
        cfg = resolve_config(
            LockConfig,
            explicit=None,
            kwargs={},
            env_prefix="GREL_LOCK_TEST_",
            env_load=None,
        )
    assert cfg.lease_duration == DEFAULT_LEASE  # flag off, env ignored

    monkeypatch.setenv("GREL_ENV_LOAD", "true")
    cfg2 = resolve_config(
        LockConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOCK_TEST_",
        env_load=None,
    )
    assert cfg2.lease_duration == LEASE_FROM_ENV  # flag on, env read
