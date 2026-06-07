"""Tests for the merged `Coordination` component (Grelmicro app integration)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grelmicro import Component, Grelmicro
from grelmicro.coordination import (
    Coordination,
    CoordinationBackendError,
    LeaderElection,
    Lock,
    TaskLock,
)
from grelmicro.coordination.memory import (
    MemoryLeaderElectionBackend,
    MemoryLockAdapter,
)
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider

if TYPE_CHECKING:
    from types import TracebackType

pytestmark = [pytest.mark.timeout(1)]


def test_coordination_satisfies_component_protocol() -> None:
    """`Coordination` is a runtime-checkable `Component`."""
    assert isinstance(Coordination(lock=MemoryLockAdapter()), Component)


def test_coordination_default_kind_and_name() -> None:
    """Default kind is `coordination` and default name is `default`."""
    coordination = Coordination(lock=MemoryLockAdapter())
    assert coordination.kind == "coordination"
    assert coordination.name == "default"


def test_coordination_named_registration() -> None:
    """A named `Coordination` component coexists with the default one."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter()),
            Coordination(lock=MemoryLockAdapter(), name="analytics"),
        ]
    )
    assert micro.get("coordination", "default").name == "default"
    assert micro.get("coordination", "analytics").name == "analytics"


def test_lock_factory_binds_backend() -> None:
    """`coordination.lock(name)` creates a `Lock` bound to the lock backend."""
    backend = MemoryLockAdapter()
    coordination = Coordination(lock=backend)
    lock = coordination.lock("cart")
    assert isinstance(lock, Lock)
    assert lock.backend is backend


def test_task_lock_factory_binds_backend() -> None:
    """`coordination.task_lock(name)` binds the lock backend."""
    backend = MemoryLockAdapter()
    coordination = Coordination(lock=backend)
    task_lock = coordination.task_lock("cleanup")
    assert isinstance(task_lock, TaskLock)
    assert task_lock.backend is backend


def test_leader_election_factory_binds_backend() -> None:
    """`coordination.leaderelection(name)` binds the election backend."""
    backend = MemoryLeaderElectionBackend()
    coordination = Coordination(election=backend)
    election = coordination.leaderelection("worker")
    assert isinstance(election, LeaderElection)
    assert election.backend is backend


def test_lock_backend_property() -> None:
    """`coordination.lock_backend` returns the wired lock backend."""
    backend = MemoryLockAdapter()
    coordination = Coordination(lock=backend)
    assert coordination.lock_backend is backend


def test_election_backend_property() -> None:
    """`coordination.election_backend` returns the wired election backend."""
    backend = MemoryLeaderElectionBackend()
    coordination = Coordination(election=backend)
    assert coordination.election_backend is backend


def test_lock_without_lock_backend_raises() -> None:
    """`coordination.lock()` raises when no lock backend is wired."""
    coordination = Coordination(election=MemoryLeaderElectionBackend())
    with pytest.raises(CoordinationBackendError, match="no lock backend"):
        coordination.lock("cart")


def test_task_lock_without_lock_backend_raises() -> None:
    """`coordination.task_lock()` raises when no lock backend is wired."""
    coordination = Coordination(election=MemoryLeaderElectionBackend())
    with pytest.raises(CoordinationBackendError, match="no lock backend"):
        coordination.task_lock("cleanup")


def test_lock_backend_property_without_lock_backend_raises() -> None:
    """`coordination.lock_backend` raises when no lock backend is wired."""
    coordination = Coordination(election=MemoryLeaderElectionBackend())
    with pytest.raises(CoordinationBackendError, match="no lock backend"):
        _ = coordination.lock_backend


def test_leader_election_without_election_backend_raises() -> None:
    """`coordination.leaderelection()` raises when no election backend."""
    coordination = Coordination(lock=MemoryLockAdapter())
    with pytest.raises(
        CoordinationBackendError, match="no leader election backend"
    ):
        coordination.leaderelection("worker")


def test_election_backend_property_without_election_backend_raises() -> None:
    """`coordination.election_backend` raises when none is wired."""
    coordination = Coordination(lock=MemoryLockAdapter())
    with pytest.raises(
        CoordinationBackendError, match="no leader election backend"
    ):
        _ = coordination.election_backend


def test_coordination_resolves_both_from_provider() -> None:
    """A positional Provider resolves both lock and election backends."""
    provider = RedisProvider("redis://localhost:6379/0")
    coordination = Coordination(provider)
    assert coordination.lock_backend.__class__.__name__ == "RedisLockAdapter"
    assert coordination.election_backend.__class__.__name__ == (
        "RedisLeaderElectionBackend"
    )


def test_coordination_accepts_bare_provider_class() -> None:
    """A zero-arg Provider class is instantiated for `source`."""

    class _ZeroArgProvider(RedisProvider):
        def __init__(self) -> None:
            super().__init__("redis://localhost:6379/0")

    coordination = Coordination(_ZeroArgProvider)
    assert coordination.lock_backend.__class__.__name__ == "RedisLockAdapter"


def test_lock_keyword_accepts_provider() -> None:
    """`lock=Provider` resolves the lock backend via `provider.lock()`."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    coordination = Coordination(lock=provider)
    assert coordination.lock_backend.__class__.__name__ == "PostgresLockAdapter"


def test_election_keyword_accepts_provider() -> None:
    """`election=Provider` resolves via `provider.leaderelection()`."""
    provider = RedisProvider("redis://localhost:6379/0")
    coordination = Coordination(election=provider)
    assert coordination.election_backend.__class__.__name__ == (
        "RedisLeaderElectionBackend"
    )


def test_lock_keyword_accepts_bare_backend_class() -> None:
    """`lock=MemoryLockAdapter` instantiates the zero-arg backend class."""
    coordination = Coordination(lock=MemoryLockAdapter)
    assert isinstance(coordination.lock_backend, MemoryLockAdapter)


def test_election_keyword_accepts_bare_backend_class() -> None:
    """`election=MemoryLeaderElectionBackend` instantiates the class."""
    coordination = Coordination(
        election=MemoryLeaderElectionBackend,  # ty: ignore[invalid-argument-type]
    )
    assert isinstance(
        coordination.election_backend, MemoryLeaderElectionBackend
    )


def test_keyword_overrides_provider_lock_backend() -> None:
    """`lock=` overrides the lock backend resolved from `source`."""
    provider = RedisProvider("redis://localhost:6379/0")
    override = MemoryLockAdapter()
    coordination = Coordination(provider, lock=override)
    assert coordination.lock_backend is override
    assert coordination.election_backend.__class__.__name__ == (
        "RedisLeaderElectionBackend"
    )


def test_keyword_overrides_provider_election_backend() -> None:
    """`election=` overrides the election backend resolved from `source`."""
    provider = RedisProvider("redis://localhost:6379/0")
    override = MemoryLeaderElectionBackend()
    coordination = Coordination(provider, election=override)
    assert coordination.election_backend is override
    assert coordination.lock_backend.__class__.__name__ == "RedisLockAdapter"


async def test_lock_only_lifecycle() -> None:
    """A lock-only component opens and closes its lock backend with the app."""
    backend = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=backend)])
    async with micro, micro.coordination.lock("k"):
        pass


async def test_election_only_lifecycle() -> None:
    """An election-only component opens and closes its election backend."""
    backend = MemoryLeaderElectionBackend()
    micro = Grelmicro(uses=[Coordination(election=backend)])
    async with micro:
        assert micro.coordination.election_backend is backend


async def test_dual_lifecycle_opens_and_closes_both() -> None:
    """Both backends open and close when both are wired."""
    lock_backend = MemoryLockAdapter()
    election_backend = MemoryLeaderElectionBackend()
    coordination = Coordination(lock=lock_backend, election=election_backend)
    async with Grelmicro(uses=[coordination]):
        assert coordination.lock_backend is lock_backend
        assert coordination.election_backend is election_backend


async def test_lock_via_micro_attribute() -> None:
    """`micro.coordination.lock(...)` is the conventional access path."""
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
    async with micro, micro.coordination.lock("cart"):
        pass


async def test_use_auto_wraps_raw_lock_backend() -> None:
    """`micro.use(MemoryLockAdapter())` auto-wraps the backend."""
    backend = MemoryLockAdapter()
    micro = Grelmicro(uses=[backend])
    assert isinstance(micro.coordination, Coordination)
    assert micro.coordination.lock_backend is backend


async def test_use_auto_wraps_raw_election_backend() -> None:
    """`micro.use(MemoryLeaderElectionBackend())` auto-wraps the backend."""
    backend = MemoryLeaderElectionBackend()
    micro = Grelmicro(uses=[backend])
    assert isinstance(micro.coordination, Coordination)
    assert micro.coordination.election_backend is backend


async def test_micro_coordination_prefers_default() -> None:
    """`micro.coordination` resolves the default-named component."""
    primary = MemoryLockAdapter()
    micro = Grelmicro(
        uses=[
            Coordination(lock=primary),
            Coordination(lock=MemoryLockAdapter(), name="analytics"),
        ]
    )
    assert micro.coordination.lock_backend is primary


async def test_micro_coordination_returns_sole_entry_when_no_default() -> None:
    """`micro.coordination` returns the only registered component."""
    only = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=only, name="primary")])
    assert micro.coordination.lock_backend is only


async def test_micro_coordination_raises_when_ambiguous() -> None:
    """`micro.coordination` raises when ambiguous with no default."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter(), name="primary"),
            Coordination(lock=MemoryLockAdapter(), name="analytics"),
        ]
    )
    with pytest.raises(
        AttributeError, match="multiple 'coordination' components"
    ):
        _ = micro.coordination


async def test_multi_backend_named_lookup() -> None:
    """Named components are reachable via `micro.get('coordination', name)`."""
    primary = MemoryLockAdapter()
    analytics = MemoryLockAdapter()
    micro = Grelmicro(
        uses=[
            Coordination(lock=primary),
            Coordination(lock=analytics, name="analytics"),
        ]
    )
    async with micro:
        async with micro.get("coordination", "analytics").lock("event"):
            pass
        assert micro.get("coordination", "analytics").lock_backend is analytics
        assert micro.get("coordination", "default").lock_backend is primary


async def test_aexit_closes_both_when_lock_close_raises() -> None:
    """`__aexit__` closes the election backend even if the lock close raises."""

    class _RaisingLock(MemoryLockAdapter):
        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            msg = "lock close failed"
            raise RuntimeError(msg)

    class _TrackingElection(MemoryLeaderElectionBackend):
        closed = False

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            type(self).closed = True
            await super().__aexit__(exc_type, exc_value, traceback)

    lock_backend = _RaisingLock()
    election_backend = _TrackingElection()
    coordination = Coordination(lock=lock_backend, election=election_backend)
    await coordination.__aenter__()
    with pytest.raises(RuntimeError, match="lock close failed"):
        await coordination.__aexit__(None, None, None)
    assert _TrackingElection.closed is True
