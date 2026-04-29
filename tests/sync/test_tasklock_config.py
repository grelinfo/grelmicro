"""Tests for the three-paths TaskLock construction."""

import pytest
from pytest_mock import MockerFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock, TaskLockConfig

MIN_KWARG = 5.0
MAX_KWARG = 30.0
MIN_ENV = 7.0
MAX_ENV = 90.0
DEFAULT_MIN = 1.0
DEFAULT_MAX = 60.0


@pytest.fixture
def backend() -> SyncBackend:
    """Return a memory backend usable without a running event loop."""
    return MemorySyncBackend()


def test_construction_does_not_touch_registry(mocker: MockerFixture) -> None:
    """`TaskLock("cart")` performs zero registry calls at construction."""
    spy = mocker.patch("grelmicro.sync.tasklock.get_sync_backend")
    TaskLock("cart")
    assert spy.call_count == 0


def test_backend_property_resolves_lazily_and_caches(
    mocker: MockerFixture,
) -> None:
    """First `task_lock.backend` access resolves once, subsequent reads hit the cache."""
    backend_instance = MemorySyncBackend(auto_register=False)
    spy = mocker.patch(
        "grelmicro.sync.tasklock.get_sync_backend",
        return_value=backend_instance,
    )
    task_lock = TaskLock("cart")
    assert spy.call_count == 0
    assert task_lock.backend is backend_instance
    assert task_lock.backend is backend_instance
    assert spy.call_count == 1


def test_programmatic_path_uses_kwargs(backend: SyncBackend) -> None:
    """Plain kwargs build a config, falling back to TaskLockConfig defaults."""
    task_lock = TaskLock(
        "cleanup",
        backend=backend,
        min_lock_seconds=MIN_KWARG,
        max_lock_seconds=MAX_KWARG,
    )
    assert task_lock.name == "cleanup"
    assert task_lock.config.min_lock_seconds == MIN_KWARG
    assert task_lock.config.max_lock_seconds == MAX_KWARG


def test_declarative_path_uses_from_config(backend: SyncBackend) -> None:
    """`TaskLock.from_config()` constructs from a name and a `TaskLockConfig`."""
    cfg = TaskLockConfig(
        worker="web-1",
        min_lock_seconds=MIN_KWARG,
        max_lock_seconds=MAX_KWARG,
    )
    task_lock = TaskLock.from_config("cleanup", cfg, backend=backend)
    assert task_lock.name == "cleanup"
    assert task_lock.config is cfg


def test_from_config_bypasses_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TaskLock.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", str(MAX_ENV))
    cfg = TaskLockConfig(
        worker="web-1",
        min_lock_seconds=MIN_KWARG,
        max_lock_seconds=MAX_KWARG,
    )
    task_lock = TaskLock.from_config("cleanup", cfg, backend=backend)
    assert task_lock.config.max_lock_seconds == MAX_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_TASK_LOCK_{NAME}_*`` populate unset fields."""
    monkeypatch.setenv("GREL_TASK_LOCK_CLEANUP_MIN_LOCK_SECONDS", str(MIN_ENV))
    monkeypatch.setenv("GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", str(MAX_ENV))
    task_lock = TaskLock("cleanup", backend=backend)
    assert task_lock.config.min_lock_seconds == MIN_ENV
    assert task_lock.config.max_lock_seconds == MAX_ENV


def test_kwargs_override_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv("GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", str(MAX_ENV))
    task_lock = TaskLock("cleanup", backend=backend, max_lock_seconds=MAX_KWARG)
    assert task_lock.config.max_lock_seconds == MAX_KWARG


def test_env_prefix_override(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_TASK_LOCK_{NAME}_``."""
    monkeypatch.setenv("MYAPP_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", str(MAX_ENV))
    task_lock = TaskLock(
        "cleanup",
        backend=backend,
        env_prefix="MYAPP_TASK_LOCK_CLEANUP_",
    )
    assert task_lock.config.max_lock_seconds == MAX_ENV


def test_read_env_false_ignores_env(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv("GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", str(MAX_ENV))
    task_lock = TaskLock("cleanup", backend=backend, read_env=False)
    assert task_lock.config.max_lock_seconds == DEFAULT_MAX


def test_zero_config_uses_taskconfig_defaults(
    backend: SyncBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, TaskLockConfig defaults take over."""
    monkeypatch.delenv("GREL_TASK_LOCK_CLEANUP_MIN_LOCK_SECONDS", raising=False)
    monkeypatch.delenv("GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS", raising=False)
    task_lock = TaskLock("cleanup", backend=backend)
    assert task_lock.config.min_lock_seconds == DEFAULT_MIN
    assert task_lock.config.max_lock_seconds == DEFAULT_MAX


def test_worker_default_factory_generates_uuid(backend: SyncBackend) -> None:
    """An auto-generated worker id is set when none is provided."""
    task_lock = TaskLock("cleanup", backend=backend)
    assert task_lock.config.worker
    assert task_lock.config.worker != ""


def test_worker_kwarg_passed_through(backend: SyncBackend) -> None:
    """An explicit worker kwarg overrides the default factory."""
    task_lock = TaskLock("cleanup", backend=backend, worker="web-1")
    assert task_lock.config.worker == "web-1"
