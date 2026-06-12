"""Tests for the three-paths LeaderElection construction."""

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.abc import LeaderElectionBackend
from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)
from grelmicro.coordination.memory import MemoryLeaderElectionBackend

LEASE_KWARG = 12.0
RETRY_KWARG = 1.0
LEASE_ENV = 30.0
RETRY_ENV = 3.0
DEFAULT_LEASE = 15.0
DEFAULT_RETRY = 2.0


@pytest.fixture
def backend() -> LeaderElectionBackend:
    """Return a memory backend usable without a running event loop."""
    return MemoryLeaderElectionBackend()


async def test_release_is_noop_when_backend_was_never_resolved() -> None:
    """`_release` returns silently when no backend was ever bound."""
    le = LeaderElection("svc")
    await le._release()


def test_construction_does_not_resolve(mocker: MockerFixture) -> None:
    """`LeaderElection("svc")` performs zero ambient resolution at construction."""
    spy = mocker.spy(Grelmicro, "current")
    LeaderElection("svc")
    assert spy.call_count == 0


async def test_backend_property_resolves_on_every_call() -> None:
    """`le.backend` consults the active `Grelmicro` app on each read."""
    backend_instance = MemoryLeaderElectionBackend()
    micro = Grelmicro(uses=[Coordination(election=backend_instance)])
    async with micro:
        le = LeaderElection("svc")
        assert le.backend is backend_instance
        assert le.backend is backend_instance


def test_programmatic_path_uses_kwargs(backend: LeaderElectionBackend) -> None:
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


def test_declarative_path_uses_from_config(
    backend: LeaderElectionBackend,
) -> None:
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
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LeaderElection.from_config()` ignores env even when set."""
    monkeypatch.setenv(
        "GREL_LEADERELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    cfg = LeaderElectionConfig(
        worker="web-1",
        lease_duration=LEASE_KWARG,
        retry_interval=RETRY_KWARG,
    )
    le = LeaderElection.from_config("cron", cfg, backend=backend)
    assert le.config.lease_duration == LEASE_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_LEADERELECTION_{NAME}_*`` populate unset fields."""
    monkeypatch.setenv(
        "GREL_LEADERELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    monkeypatch.setenv(
        "GREL_LEADERELECTION_CRON_RETRY_INTERVAL", str(RETRY_ENV)
    )
    le = LeaderElection("cron", backend=backend)
    assert le.config.lease_duration == LEASE_ENV
    assert le.config.retry_interval == RETRY_ENV


def test_kwargs_override_env(
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv(
        "GREL_LEADERELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection("cron", backend=backend, lease_duration=LEASE_KWARG)
    assert le.config.lease_duration == LEASE_KWARG


def test_env_prefix_override(
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_LEADERELECTION_{NAME}_``."""
    monkeypatch.setenv(
        "MYAPP_LEADER_ELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection(
        "cron",
        backend=backend,
        env_prefix="MYAPP_LEADER_ELECTION_CRON_",
    )
    assert le.config.lease_duration == LEASE_ENV


def test_env_load_false_ignores_env(
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_load=False`` skips env reads entirely."""
    monkeypatch.setenv(
        "GREL_LEADERELECTION_CRON_LEASE_DURATION", str(LEASE_ENV)
    )
    le = LeaderElection("cron", backend=backend, env_load=False)
    assert le.config.lease_duration == DEFAULT_LEASE


def test_zero_config_uses_leaderelectionconfig_defaults(
    backend: LeaderElectionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, LeaderElectionConfig defaults take over."""
    monkeypatch.delenv("GREL_LEADERELECTION_CRON_LEASE_DURATION", raising=False)
    monkeypatch.delenv("GREL_LEADERELECTION_CRON_RETRY_INTERVAL", raising=False)
    le = LeaderElection("cron", backend=backend)
    assert le.config.lease_duration == DEFAULT_LEASE
    assert le.config.retry_interval == DEFAULT_RETRY


def test_worker_default_factory_generates_uuid(
    backend: LeaderElectionBackend,
) -> None:
    """An auto-generated worker id is set when none is provided."""
    le = LeaderElection("cron", backend=backend)
    assert le.config.worker
    assert le.config.worker != ""


def test_worker_kwarg_passed_through(backend: LeaderElectionBackend) -> None:
    """An explicit worker kwarg overrides the default factory."""
    le = LeaderElection("cron", backend=backend, worker="web-1")
    assert le.config.worker == "web-1"


# --- retry_jitter validation ---


def test_leaderelectionconfig_retry_jitter_default() -> None:
    """LeaderElectionConfig default retry_jitter is 0.1."""
    cfg = LeaderElectionConfig(worker="test")
    assert cfg.retry_jitter == 0.1  # noqa: PLR2004


def test_leaderelectionconfig_retry_jitter_zero_accepted() -> None:
    """LeaderElectionConfig accepts retry_jitter=0 to disable jitter."""
    cfg = LeaderElectionConfig(worker="test", retry_jitter=0.0)
    assert cfg.retry_jitter == 0.0


def test_leaderelectionconfig_retry_jitter_one_rejected() -> None:
    """LeaderElectionConfig rejects retry_jitter=1."""
    with pytest.raises(ValidationError, match="retry_jitter must be"):
        LeaderElectionConfig(worker="test", retry_jitter=1.0)


def test_leaderelectionconfig_retry_jitter_above_one_rejected() -> None:
    """LeaderElectionConfig rejects retry_jitter > 1."""
    with pytest.raises(ValidationError, match="retry_jitter must be"):
        LeaderElectionConfig(worker="test", retry_jitter=1.5)


def test_leaderelectionconfig_retry_jitter_negative_rejected() -> None:
    """LeaderElectionConfig rejects retry_jitter < 0."""
    with pytest.raises(ValidationError, match="retry_jitter must be"):
        LeaderElectionConfig(worker="test", retry_jitter=-0.1)
