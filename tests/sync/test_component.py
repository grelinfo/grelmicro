"""Tests for the `Sync` component (Grelmicro app integration)."""

from __future__ import annotations

import pytest

from grelmicro import Component, Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.sync import Lock, Sync, TaskLock
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.sync.postgres import PostgresSyncAdapter
from grelmicro.sync.redis import RedisSyncAdapter


def test_sync_satisfies_component_protocol() -> None:
    """`Sync` is a runtime-checkable `Component`."""
    assert isinstance(Sync(MemorySyncAdapter()), Component)


def test_sync_default_kind_and_name() -> None:
    """Default kind is `sync` and default name is `default`."""
    sync = Sync(MemorySyncAdapter())
    assert sync.kind == "sync"
    assert sync.name == "default"


def test_sync_named_registration() -> None:
    """A named `Sync` component coexists with the default one."""
    micro = Grelmicro(
        uses=[
            Sync(MemorySyncAdapter()),
            Sync(MemorySyncAdapter(), name="analytics"),
        ]
    )
    assert micro.get("sync", "default").name == "default"
    assert micro.get("sync", "analytics").name == "analytics"


def test_sync_lock_factory_binds_backend() -> None:
    """`sync.lock(name)` creates a `Lock` bound to the wrapped backend."""
    backend = MemorySyncAdapter()
    sync = Sync(backend)
    lock = sync.lock("cart")
    assert isinstance(lock, Lock)
    assert lock.backend is backend


def test_sync_task_lock_factory_binds_backend() -> None:
    """`sync.task_lock(name)` creates a `TaskLock` bound to the wrapped backend."""
    backend = MemorySyncAdapter()
    sync = Sync(backend)
    task_lock = sync.task_lock("cleanup")
    assert isinstance(task_lock, TaskLock)
    assert task_lock.backend is backend


def test_sync_backend_property() -> None:
    """`sync.backend` returns the wrapped backend."""
    backend = MemorySyncAdapter()
    sync = Sync(backend)
    assert sync.backend is backend


async def test_sync_opens_and_closes_backend_with_app() -> None:
    """`async with micro:` opens and closes the underlying backend."""
    backend = MemorySyncAdapter()
    sync = Sync(backend)
    micro = Grelmicro(uses=[sync])
    async with micro, sync.lock("k"):
        # Backend is open: a Lock can be acquired.
        pass


async def test_sync_lock_via_micro_attribute() -> None:
    """`micro.sync.lock(...)` is the conventional access path."""
    micro = Grelmicro(uses=[Sync(MemorySyncAdapter())])
    async with micro, micro.sync.lock("cart"):
        pass


async def test_use_auto_wraps_raw_sync_backend() -> None:
    """`micro.use(MemorySyncAdapter())` auto-wraps the backend in `Sync`."""
    backend = MemorySyncAdapter()
    micro = Grelmicro(uses=[backend])
    assert isinstance(micro.sync, Sync)
    assert micro.sync.backend is backend


async def test_use_auto_wrap_lifecycles_backend() -> None:
    """Auto-wrapped backend opens and closes with the app."""
    backend = MemorySyncAdapter()
    micro = Grelmicro(uses=[backend])
    async with micro, micro.sync.lock("k"):
        pass


async def test_micro_sync_prefers_default_when_named_also_registered() -> None:
    """`micro.sync` resolves to the `(sync, default)` component even after named registrations."""
    primary = MemorySyncAdapter()
    analytics = MemorySyncAdapter()
    micro = Grelmicro(
        uses=[
            Sync(primary),
            Sync(analytics, name="analytics"),
        ]
    )
    assert micro.sync.backend is primary


async def test_micro_sync_returns_sole_entry_when_no_default() -> None:
    """`micro.sync` returns the only registered Sync when no default exists."""
    only = MemorySyncAdapter()
    micro = Grelmicro(uses=[Sync(only, name="primary")])
    assert micro.sync.backend is only


async def test_micro_sync_raises_when_ambiguous() -> None:
    """`micro.sync` raises when multiple non-default components exist with no default."""
    micro = Grelmicro(
        uses=[
            Sync(MemorySyncAdapter(), name="primary"),
            Sync(MemorySyncAdapter(), name="analytics"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'sync' components"):
        _ = micro.sync


async def test_sync_multi_backend_named_lookup() -> None:
    """Named `Sync` components are reachable via `micro.get('sync', name)`."""
    primary = MemorySyncAdapter()
    analytics = MemorySyncAdapter()
    micro = Grelmicro(
        uses=[
            Sync(primary),
            Sync(analytics, name="analytics"),
        ]
    )
    async with micro:
        async with micro.get("sync", "analytics").lock("event"):
            pass
        assert micro.get("sync", "analytics").backend is analytics
        assert micro.get("sync", "default").backend is primary


def test_sync_accepts_redis_provider() -> None:
    """`Sync(RedisProvider(...))` calls `provider.sync()` to build the adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    sync = Sync(provider)
    assert isinstance(sync.backend, RedisSyncAdapter)
    assert sync.backend.provider is provider


def test_sync_accepts_postgres_provider() -> None:
    """`Sync(PostgresProvider(...))` calls `provider.sync()` to build the adapter."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    sync = Sync(provider)
    assert isinstance(sync.backend, PostgresSyncAdapter)
    assert sync.backend.provider is provider


async def test_sync_accepts_bare_backend_class() -> None:
    """`Sync(MemorySyncAdapter)` instantiates the zero-arg backend class."""
    component = Sync(MemorySyncAdapter)
    assert isinstance(component.backend, MemorySyncAdapter)
