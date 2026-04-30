"""Tests for the three-paths LeaderElection construction."""

import pytest
from pytest_mock import MockerFixture

from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.leaderelection import LeaderElection, LeaderElectionConfig
from grelmicro.sync.memory import MemorySyncBackend

LEASE_KWARG = 12.0
RETRY_KWARG = 1.0
LEASE_ENV = 30.0
RETRY_ENV = 3.0
DEFAULT_LEASE = 15.0
DEFAULT_RETRY = 2.0


@pytest.fixture
def backend() -> SyncBackend:
    """Return a memory backend usable without a running event loop."""
    return MemorySyncBackend()


@pytest.mark.anyio
async def test_release_is_noop_when_backend_was_never_resolved() -> None:
    """`_release` returns silently when no backend was ever bound."""
    le = LeaderElection("svc")
    await le._release()


def test_construction_does_not_touch_registry(mocker: MockerFixture) -> None:
    """`LeaderElection("svc")` performs zero registry calls at construction."""
    spy = mocker.patch.object(sync_backend_registry, "get")
    LeaderElection("svc")
    assert spy.call_count == 0


def test_backend_property_resolves_on_every_call(
    mocker: MockerFixture,
) -> None:
    """First `le.backend` access resolves once, subsequent reads hit the cache."""
    backend_instance = MemorySyncBackend()
    spy = mocker.patch(
        "grelmicro.sync.leaderelection.get_sync_backend",
        return_value=backend_instance,
    )
    le = LeaderElection("svc")
    assert spy.call_count == 0
    assert le.backend is backend_instance
    assert le.backend is backend_instance
    expected_calls = 2
    assert spy.call_count == expected_calls


def test_programmatic_path_uses_kwargs(backend: SyncBackend) -> None:
    """Plain kwargs build a config, falling back to LeaderElectionConfig defaults."""
    le = LeaderElection(
        "cron",
        backend=backend,
        lease_duration=LEASE_KWARG,
        retry_interval=RETRY_KWARG,
    )
    assert le.name == "cron"
    assert le.config.lease_duration == LEASE_KWARG
    assert le.config.retry_interval == RETRY_KWARG


def test_declarative_path_uses_from_config(backend: SyncBackend) -> None:
    """`LeaderElection.from_config()` constructs from a name and a `LeaderElectionConfig`."""
    cfg = LeaderElectionConfig(
        worker="web-1",
        lease_duration=LEASE_KWARG,
        retry_interval=RETRY_KWARG,
    )
    le = LeaderElection.from_config("cron", cfg, backend=backend)
    assert le.name == "cron"
    assert le.config is cfg


def test_from_config_bypasses_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LeaderElection.from_config()` ignores env even when set."""
    monkeypatch.setenv(
        "GREL_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    cfg = LeaderElectionConfig(
        worker="web-1",
        lease_duration=LEASE_KWARG,
        retry_interval=RETRY_KWARG,
    )
    le = LeaderElection.from_config("cron", cfg, backend=backend)
    assert le.config.lease_duration == LEASE_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_LEADER_ELECTION_{NAME}_*`` populate unset fields."""
    monkeypatch.setenv(
        "GREL_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    monkeypatch.setenv(
        "GREL_LEADER_ELECTION_CRON_RETRY_INTERVAL", str(RETRY_ENV)
    )
    le = LeaderElection("cron", backend=backend)
    assert le.config.lease_duration == LEASE_ENV
    assert le.config.retry_interval == RETRY_ENV


def test_kwargs_override_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv(
        "GREL_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection("cron", backend=backend, lease_duration=LEASE_KWARG)
    assert le.config.lease_duration == LEASE_KWARG


def test_env_prefix_override(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_LEADER_ELECTION_{NAME}_``."""
    monkeypatch.setenv(
        "MYAPP_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection(
        "cron",
        backend=backend,
        env_prefix="MYAPP_LEADER_ELECTION_CRON_",
    )
    assert le.config.lease_duration == LEASE_ENV


def test_read_env_false_ignores_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv(
        "GREL_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection("cron", backend=backend, read_env=False)
    assert le.config.lease_duration == DEFAULT_LEASE


def test_zero_config_uses_leaderelectionconfig_defaults(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, LeaderElectionConfig defaults take over."""
    monkeypatch.delenv(
        "GREL_LEADER_ELECTION_CRON_LEASE_DURATION", raising=False
    )
    monkeypatch.delenv(
        "GREL_LEADER_ELECTION_CRON_RETRY_INTERVAL", raising=False
    )
    le = LeaderElection("cron", backend=backend)
    assert le.config.lease_duration == DEFAULT_LEASE
    assert le.config.retry_interval == DEFAULT_RETRY


def test_worker_default_factory_generates_uuid(backend: SyncBackend) -> None:
    """An auto-generated worker id is set when none is provided."""
    le = LeaderElection("cron", backend=backend)
    assert le.config.worker
    assert le.config.worker != ""


def test_worker_kwarg_passed_through(backend: SyncBackend) -> None:
    """An explicit worker kwarg overrides the default factory."""
    le = LeaderElection("cron", backend=backend, worker="web-1")
    assert le.config.worker == "web-1"
