"""Tests for what a component accepts as the source of its backend."""

from collections.abc import Callable
from typing import Any, Self

import pytest

from grelmicro import AmbiguousBackendError, BackendScopeError, Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.outbox import Outbox
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import CircuitBreakerComponent, RateLimiterComponent
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter


@pytest.mark.parametrize(
    ("adapter", "slot"),
    [
        (MemoryLockAdapter, "lock_backend"),
        (MemoryReadWriteLockAdapter, "readwritelock_backend"),
        (MemoryLeaderElectionAdapter, "leaderelection_backend"),
        (MemoryScheduleAdapter, "schedule_backend"),
    ],
)
def test_coordination_wires_a_backend_into_the_slot_it_serves(
    adapter: type, slot: str
) -> None:
    """The first argument says where the backend comes from, as elsewhere."""
    backend = adapter()

    coordination = Coordination(backend)

    assert getattr(coordination, slot) is backend


def test_coordination_takes_a_backend_class_as_well() -> None:
    """A zero-argument class is built for you, as on every component."""
    coordination = Coordination(MemoryLockAdapter)

    assert isinstance(coordination.lock_backend, MemoryLockAdapter)


def test_a_keyword_overrides_the_backend_the_source_filled() -> None:
    """`lock=` still wins over the first argument."""
    election = MemoryLeaderElectionAdapter()
    lock = MemoryLockAdapter()

    coordination = Coordination(election, lock=lock)

    assert coordination.leaderelection_backend is election
    assert coordination.lock_backend is lock


def test_a_coordination_wired_from_a_backend_is_checked() -> None:
    """The scope check sees the backend, instead of an empty component."""
    micro = Grelmicro(uses=[Coordination(MemoryLockAdapter())])

    with pytest.raises(BackendScopeError, match="MemoryLockAdapter"):
        micro.check_backends()


def test_a_provider_that_breaks_building_a_backend_is_not_hidden() -> None:
    """Only a kind the Provider does not ship leaves a slot empty."""

    class Broken(MemoryProvider):
        def lock_backend(self, **_: object) -> MemoryLockAdapter:
            msg = "typo"
            raise AttributeError(msg)

    with pytest.raises(AttributeError, match="typo"):
        Coordination(Broken())


_WRONG_KIND: list[tuple[Callable[..., object], object, str]] = [
    (
        Cache,
        MemoryLockAdapter(),
        (
            "Cache expects a Provider or a CacheBackend, got MemoryLockAdapter, "
            "which is a LockBackend. Pass it to Coordination(lock=...) instead."
        ),
    ),
    (
        Coordination,
        MemoryCacheAdapter(),
        (
            "Coordination expects a Provider or a coordination backend, got "
            "MemoryCacheAdapter, which is a CacheBackend. Pass it to Cache "
            "instead."
        ),
    ),
    (
        RateLimiterComponent,
        MemoryCacheAdapter(),
        (
            "RateLimiterComponent expects a Provider or a RateLimiterBackend, "
            "got MemoryCacheAdapter"
        ),
    ),
    (
        CircuitBreakerComponent,
        MemoryRateLimiterAdapter(),
        (
            "CircuitBreakerComponent expects a Provider or a "
            "CircuitBreakerBackend, got MemoryRateLimiterAdapter, which is a "
            "RateLimiterBackend. Pass it to RateLimiterComponent instead."
        ),
    ),
    (
        Outbox,
        MemoryCacheAdapter(),
        "Outbox expects a Provider or an OutboxBackend, got MemoryCacheAdapter",
    ),
    (
        Cache,
        "redis://localhost:6379/0",
        "Cache expects a Provider or a CacheBackend, got str.",
    ),
]


@pytest.mark.parametrize(
    ("component", "source", "message"),
    _WRONG_KIND,
    ids=[
        f"{getattr(c, '__name__', c)}-{type(s).__name__}"
        for c, s, _ in _WRONG_KIND
    ],
)
def test_a_source_of_the_wrong_kind_is_refused_at_construction(
    component: Callable[..., object], source: object, message: str
) -> None:
    """A backend for another component fails where it is passed."""
    with pytest.raises(TypeError) as error:
        component(source)

    assert message in str(error.value)


class _LockAndElection:
    """Satisfies `LockBackend` and `LeaderElectionBackend`, neither subsuming."""

    _loop = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def acquire(self, *args: object, **kwargs: object) -> None: ...
    async def locked(self, *args: object, **kwargs: object) -> None: ...
    async def owned(self, *args: object, **kwargs: object) -> None: ...
    async def release(self, *args: object, **kwargs: object) -> None: ...
    async def get(self, *args: object, **kwargs: object) -> None: ...
    async def acquire_or_renew(
        self, *args: object, **kwargs: object
    ) -> None: ...


def _both_kinds() -> Any:  # noqa: ANN401
    """Return the two-kind backend as a caller without type checks passes it."""
    return _LockAndElection()


def test_coordination_refuses_a_backend_serving_two_slots() -> None:
    """Only the caller knows which slot was meant, so the keyword says it."""
    with pytest.raises(
        AmbiguousBackendError, match=r"Coordination\(lock=\.\.\.\)"
    ):
        Coordination(_both_kinds())


def test_a_backend_of_two_other_kinds_is_refused_without_a_guess() -> None:
    """The error names no component when two could take the backend."""
    with pytest.raises(TypeError) as error:
        Cache(_both_kinds())

    assert str(error.value) == (
        "Cache expects a Provider or a CacheBackend, got _LockAndElection."
    )
