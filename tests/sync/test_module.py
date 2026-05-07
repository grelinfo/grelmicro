"""Tests for the `Sync` module (Grelmicro app integration)."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro, Module
from grelmicro.sync import LeaderElection, Lock, Sync, TaskLock
from grelmicro.sync.memory import MemorySyncBackend


def test_sync_satisfies_module_protocol() -> None:
    """`Sync` is a runtime-checkable `Module`."""
    assert isinstance(Sync(MemorySyncBackend()), Module)


def test_sync_default_kind_and_name() -> None:
    """Default kind is `sync` and default name is `default`."""
    sync = Sync(MemorySyncBackend())
    assert sync.kind == "sync"
    assert sync.name == "default"


def test_sync_named_registration() -> None:
    """A named `Sync` module coexists with the default one."""
    micro = Grelmicro(
        modules=[
            Sync(MemorySyncBackend()),
            Sync(MemorySyncBackend(), name="analytics"),
        ]
    )
    assert micro.get("sync", "default").name == "default"
    assert micro.get("sync", "analytics").name == "analytics"


def test_sync_lock_factory_binds_backend() -> None:
    """`sync.lock(name)` creates a `Lock` bound to the wrapped backend."""
    backend = MemorySyncBackend()
    sync = Sync(backend)
    lock = sync.lock("cart")
    assert isinstance(lock, Lock)
    assert lock.backend is backend


def test_sync_task_lock_factory_binds_backend() -> None:
    """`sync.task_lock(name)` creates a `TaskLock` bound to the wrapped backend."""
    backend = MemorySyncBackend()
    sync = Sync(backend)
    task_lock = sync.task_lock("cleanup")
    assert isinstance(task_lock, TaskLock)
    assert task_lock.backend is backend


def test_sync_leader_election_factory_binds_backend() -> None:
    """`sync.leader_election(name)` creates a `LeaderElection` bound to the backend."""
    backend = MemorySyncBackend()
    sync = Sync(backend)
    election = sync.leader_election("primary")
    assert isinstance(election, LeaderElection)
    assert election.backend is backend


def test_sync_backend_property() -> None:
    """`sync.backend` returns the wrapped backend."""
    backend = MemorySyncBackend()
    sync = Sync(backend)
    assert sync.backend is backend


async def test_sync_opens_and_closes_backend_with_app() -> None:
    """`async with micro:` opens and closes the underlying backend."""
    backend = MemorySyncBackend()
    sync = Sync(backend)
    micro = Grelmicro(modules=[sync])
    async with micro, sync.lock("k"):
        # Backend is open: a Lock can be acquired.
        pass


async def test_sync_lock_via_micro_attribute() -> None:
    """`micro.sync.lock(...)` is the conventional access path."""
    micro = Grelmicro(modules=[Sync(MemorySyncBackend())])
    async with micro, micro.sync.lock("cart"):
        pass


async def test_micro_sync_prefers_default_when_named_also_registered() -> None:
    """`micro.sync` resolves to the `(sync, default)` module even after named registrations."""
    primary = MemorySyncBackend()
    analytics = MemorySyncBackend()
    micro = Grelmicro(
        modules=[
            Sync(primary),
            Sync(analytics, name="analytics"),
        ]
    )
    assert micro.sync.backend is primary


async def test_micro_sync_returns_sole_entry_when_no_default() -> None:
    """`micro.sync` returns the only registered Sync when no default exists."""
    only = MemorySyncBackend()
    micro = Grelmicro(modules=[Sync(only, name="primary")])
    assert micro.sync.backend is only


async def test_micro_sync_raises_when_ambiguous() -> None:
    """`micro.sync` raises when multiple non-default modules exist with no default."""
    micro = Grelmicro(
        modules=[
            Sync(MemorySyncBackend(), name="primary"),
            Sync(MemorySyncBackend(), name="analytics"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'sync' modules"):
        _ = micro.sync


async def test_sync_multi_backend_named_lookup() -> None:
    """Named `Sync` modules are reachable via `micro.get('sync', name)`."""
    primary = MemorySyncBackend()
    analytics = MemorySyncBackend()
    micro = Grelmicro(
        modules=[
            Sync(primary),
            Sync(analytics, name="analytics"),
        ]
    )
    async with micro:
        async with micro.get("sync", "analytics").lock("event"):
            pass
        assert micro.get("sync", "analytics").backend is analytics
        assert micro.get("sync", "default").backend is primary
