"""Tests for the three-paths Lock construction."""

import pytest

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.lock import Lock, LockConfig
from grelmicro.sync.memory import MemorySyncBackend

LEASE_KWARG = 30.0
LEASE_ENV = 120.0
RETRY_ENV = 0.25
DEFAULT_LEASE = 60.0
DEFAULT_RETRY = 0.1


@pytest.fixture
def backend() -> SyncBackend:
    """Return a memory backend usable without a running event loop."""
    return MemorySyncBackend()


def test_programmatic_path_uses_kwargs(backend: SyncBackend) -> None:
    """Plain kwargs build a config, falling back to LockConfig defaults."""
    lock = Lock("cart", backend=backend, lease_duration=LEASE_KWARG)
    assert lock.name == "cart"
    assert lock.config.lease_duration == LEASE_KWARG
    assert lock.config.retry_interval == DEFAULT_RETRY


def test_declarative_path_uses_from_config(
    backend: SyncBackend,
) -> None:
    """`Lock.from_config()` constructs from a name and a `LockConfig`."""
    cfg = LockConfig(
        worker="web-1",
        lease_duration=LEASE_KWARG,
        retry_interval=DEFAULT_RETRY,
    )
    lock = Lock.from_config("cart", cfg, backend=backend)
    assert lock.name == "cart"
    assert lock.config is cfg


def test_from_config_bypasses_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Lock.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_ENV))
    cfg = LockConfig(
        worker="web-1",
        lease_duration=LEASE_KWARG,
        retry_interval=DEFAULT_RETRY,
    )
    lock = Lock.from_config("cart", cfg, backend=backend)
    assert lock.config.lease_duration == LEASE_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_LOCK_{NAME}_*`` populate unset fields."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_ENV))
    monkeypatch.setenv("GREL_LOCK_CART_RETRY_INTERVAL", str(RETRY_ENV))
    lock = Lock("cart", backend=backend)
    assert lock.config.lease_duration == LEASE_ENV
    assert lock.config.retry_interval == RETRY_ENV


def test_kwargs_override_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_ENV))
    lock = Lock("cart", backend=backend, lease_duration=LEASE_KWARG)
    assert lock.config.lease_duration == LEASE_KWARG


def test_env_prefix_override(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_LOCK_{NAME}_``."""
    monkeypatch.setenv("MYAPP_LOCK_CART_LEASE_DURATION", str(LEASE_ENV))
    lock = Lock(
        "cart",
        backend=backend,
        env_prefix="MYAPP_LOCK_CART_",
    )
    assert lock.config.lease_duration == LEASE_ENV


def test_read_env_false_ignores_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_ENV))
    lock = Lock("cart", backend=backend, read_env=False)
    assert lock.config.lease_duration == DEFAULT_LEASE


def test_zero_config_uses_lockconfig_defaults(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, LockConfig defaults take over."""
    monkeypatch.delenv("GREL_LOCK_CART_LEASE_DURATION", raising=False)
    monkeypatch.delenv("GREL_LOCK_CART_RETRY_INTERVAL", raising=False)
    lock = Lock("cart", backend=backend)
    assert lock.config.lease_duration == DEFAULT_LEASE
    assert lock.config.retry_interval == DEFAULT_RETRY


def test_worker_default_factory_generates_uuid(
    backend: SyncBackend,
) -> None:
    """An auto-generated worker id is set when none is provided."""
    lock = Lock("cart", backend=backend)
    assert lock.config.worker
    assert lock.config.worker != ""


def test_worker_kwarg_passed_through(backend: SyncBackend) -> None:
    """An explicit worker kwarg overrides the default factory."""
    lock = Lock("cart", backend=backend, worker="web-1")
    assert lock.config.worker == "web-1"
