"""Auto-instrumentation tests for the coordination primitives.

A distributed lock fails quietly. The tests below prove every way it can
fail reaches a counter: a lock another worker holds, a backend that is
unreachable, and a lease lost before the work under it finished.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import pytest

from grelmicro.coordination.errors import (
    LockAcquireError,
    LockNotOwnedError,
)
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
)
from grelmicro.coordination.readwritelock import ReadWriteLock
from grelmicro.coordination.tasklock import TaskLock

if TYPE_CHECKING:
    import asyncio
    from types import TracebackType

    from tests.metrics.conftest import MetricsHarness

pytestmark = [pytest.mark.timeout(5)]

LEASE = 5.0


class _FailingLockBackend:
    """Lock backend whose every call raises."""

    _loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        raise RuntimeError(name or token or duration)

    async def release(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)

    async def locked(self, *, name: str) -> bool:
        raise RuntimeError(name)

    async def owned(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)


def _outcomes(
    metrics_reader: MetricsHarness, name: str
) -> set[tuple[str, str]]:
    """Return the (mode, outcome) pairs recorded on `name`."""
    return {
        (
            str(attrs["grelmicro.lock.mode"]),
            str(attrs["grelmicro.outcome"]),
        )
        for _, attrs in metrics_reader.points(name)
    }


async def test_lock_counts_an_acquired_attempt_and_holds_the_gauge(
    metrics_reader: MetricsHarness,
) -> None:
    """Taking and releasing a lock counts an attempt and moves the holders."""
    async with MemoryLockAdapter() as backend:
        lock = Lock("orders", backend=backend, lease_duration=LEASE)
        async with lock:
            assert metrics_reader.points("grelmicro.lock.holders")[0][0] == 1

    assert _outcomes(metrics_reader, "grelmicro.lock.attempts") == {
        ("exclusive", "acquired")
    }
    attrs = metrics_reader.points("grelmicro.lock.attempts")[0][1]
    assert attrs["grelmicro.lock.name"] == "orders"
    assert metrics_reader.points("grelmicro.lock.holders")[0][0] == 0


async def test_lock_counts_an_unavailable_attempt(
    metrics_reader: MetricsHarness,
) -> None:
    """A lock another worker holds counts as unavailable, never as an error."""
    async with MemoryLockAdapter() as backend:
        holder = Lock(
            "orders", backend=backend, worker="a", lease_duration=LEASE
        )
        rival = Lock(
            "orders", backend=backend, worker="b", lease_duration=LEASE
        )
        async with holder:
            with pytest.raises(Exception, match="not acquired"):
                await rival.acquire_nowait()

    assert ("exclusive", "unavailable") in _outcomes(
        metrics_reader, "grelmicro.lock.attempts"
    )


async def test_lock_counts_a_backend_error_attempt(
    metrics_reader: MetricsHarness,
) -> None:
    """An unreachable backend counts as an error, so it can be alerted on."""
    lock = Lock("orders", backend=_FailingLockBackend(), lease_duration=LEASE)

    with pytest.raises(LockAcquireError):
        await lock.acquire_nowait()

    assert _outcomes(metrics_reader, "grelmicro.lock.attempts") == {
        ("exclusive", "error")
    }


async def test_lock_counts_a_renewal(metrics_reader: MetricsHarness) -> None:
    """Extending a held lease counts a successful renewal."""
    async with MemoryLockAdapter() as backend:
        lock = Lock("orders", backend=backend, lease_duration=LEASE)
        async with lock:
            await lock.extend()

    assert _outcomes(metrics_reader, "grelmicro.lock.renewals") == {
        ("exclusive", "success")
    }


async def test_lock_counts_a_lost_renewal(
    metrics_reader: MetricsHarness,
) -> None:
    """A lease another worker took over counts as lost, not as an error."""

    class _Gone(MemoryLockAdapter):
        async def acquire(self, **kwargs: object) -> int | None:  # noqa: ARG002
            return None

    async with MemoryLockAdapter() as backend:
        lock = Lock("orders", backend=backend, lease_duration=LEASE)
        async with lock:
            lock._backend = _Gone()
            with pytest.raises(LockNotOwnedError):
                await lock.extend()
            lock._backend = backend

    assert ("exclusive", "lost") in _outcomes(
        metrics_reader, "grelmicro.lock.renewals"
    )


async def test_lock_counts_a_renewal_the_backend_refused(
    metrics_reader: MetricsHarness,
) -> None:
    """A renewal that never reached the backend counts as an error."""
    async with MemoryLockAdapter() as backend:
        lock = Lock("orders", backend=backend, lease_duration=LEASE)
        async with lock:
            lock._backend = _FailingLockBackend()
            with pytest.raises(LockAcquireError):
                await lock.extend()
            lock._backend = backend

    assert ("exclusive", "error") in _outcomes(
        metrics_reader, "grelmicro.lock.renewals"
    )


async def test_read_and_write_leases_are_told_apart(
    metrics_reader: MetricsHarness,
) -> None:
    """The mode attribute separates a read lease from a write lease."""
    async with MemoryReadWriteLockAdapter() as backend:
        lock = ReadWriteLock("catalog", backend=backend, lease_duration=LEASE)
        async with lock.read:
            pass
        async with lock.write:
            pass

    assert _outcomes(metrics_reader, "grelmicro.lock.attempts") == {
        ("read", "acquired"),
        ("write", "acquired"),
    }


async def test_read_and_write_renewals_are_told_apart(
    metrics_reader: MetricsHarness,
) -> None:
    """Extending either lease counts a renewal under its own mode."""
    async with MemoryReadWriteLockAdapter() as backend:
        lock = ReadWriteLock("catalog", backend=backend, lease_duration=LEASE)
        async with lock.read:
            await lock.read.extend()
        async with lock.write:
            await lock.write.extend()

    assert _outcomes(metrics_reader, "grelmicro.lock.renewals") == {
        ("read", "success"),
        ("write", "success"),
    }


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_a_lease_lost_on_renewal_is_counted(
    metrics_reader: MetricsHarness, mode: str
) -> None:
    """A read or write lease taken over mid-work counts as lost."""

    class _Gone(MemoryReadWriteLockAdapter):
        async def acquire_read(self, **kwargs: object) -> int | None:  # noqa: ARG002
            return None

        async def acquire_write(self, **kwargs: object) -> Any:  # noqa: ANN401, ARG002
            return None

    async with MemoryReadWriteLockAdapter() as backend:
        lock = ReadWriteLock("catalog", backend=backend, lease_duration=LEASE)
        side = getattr(lock, mode)
        async with side:
            lock._backend = _Gone()
            with pytest.raises(LockNotOwnedError):
                await side.extend()
            lock._backend = backend

    assert (mode, "lost") in _outcomes(
        metrics_reader, "grelmicro.lock.renewals"
    )


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_a_renewal_the_backend_refused_is_counted(
    metrics_reader: MetricsHarness, mode: str
) -> None:
    """A read or write renewal that never reached the backend is an error."""

    class _Failing(MemoryReadWriteLockAdapter):
        async def acquire_read(self, **kwargs: object) -> int | None:
            raise RuntimeError(kwargs)

        async def acquire_write(self, **kwargs: object) -> Any:  # noqa: ANN401
            raise RuntimeError(kwargs)

    async with MemoryReadWriteLockAdapter() as backend:
        lock = ReadWriteLock("catalog", backend=backend, lease_duration=LEASE)
        side = getattr(lock, mode)
        async with side:
            lock._backend = _Failing()
            with pytest.raises(LockAcquireError):
                await side.extend()
            lock._backend = backend

    assert (mode, "error") in _outcomes(
        metrics_reader, "grelmicro.lock.renewals"
    )


async def test_task_lock_counts_its_own_mode(
    metrics_reader: MetricsHarness,
) -> None:
    """A task lock reports under the `task` mode, apart from a plain lock."""
    async with MemoryLockAdapter() as backend:
        lock = TaskLock(
            "sweep",
            backend=backend,
            lease_duration=LEASE,
            min_hold_duration=0.01,
        )
        async with lock:
            await lock.refresh()

    assert _outcomes(metrics_reader, "grelmicro.lock.attempts") == {
        ("task", "acquired")
    }
    assert _outcomes(metrics_reader, "grelmicro.lock.renewals") == {
        ("task", "success")
    }
    assert metrics_reader.points("grelmicro.lock.holders")[0][0] == 0


async def test_task_lock_counts_an_unavailable_attempt(
    metrics_reader: MetricsHarness,
) -> None:
    """A fire another worker already claimed counts as unavailable."""
    async with MemoryLockAdapter() as backend:
        holder = TaskLock(
            "sweep", backend=backend, worker="a", lease_duration=LEASE
        )
        rival = TaskLock(
            "sweep", backend=backend, worker="b", lease_duration=LEASE
        )
        async with holder:
            with pytest.raises(Exception, match="not acquired"):
                await rival.__aenter__()

    assert ("task", "unavailable") in _outcomes(
        metrics_reader, "grelmicro.lock.attempts"
    )


async def test_task_lock_counts_a_backend_error(
    metrics_reader: MetricsHarness,
) -> None:
    """An unreachable backend counts an error on the acquire and the renewal."""
    lock = TaskLock(
        "sweep", backend=_FailingLockBackend(), lease_duration=LEASE
    )

    with pytest.raises(LockAcquireError):
        await lock.__aenter__()

    assert ("task", "error") in _outcomes(
        metrics_reader, "grelmicro.lock.attempts"
    )


async def test_task_lock_counts_a_lost_renewal(
    metrics_reader: MetricsHarness,
) -> None:
    """A lease gone before `refresh` counts as lost, not as an error."""

    class _Gone(MemoryLockAdapter):
        async def acquire(self, **kwargs: object) -> int | None:  # noqa: ARG002
            return None

    async with MemoryLockAdapter() as backend:
        lock = TaskLock("sweep", backend=backend, lease_duration=LEASE)
        await lock.__aenter__()
        lock._backend = _Gone()
        with pytest.raises(LockNotOwnedError):
            await lock.refresh()

    assert ("task", "lost") in _outcomes(
        metrics_reader, "grelmicro.lock.renewals"
    )


async def test_leader_election_counts_the_leader(
    metrics_reader: MetricsHarness,
) -> None:
    """The elected worker counts an acquired attempt and reads 1 on the gauge."""
    async with MemoryLeaderElectionAdapter() as backend:
        election = LeaderElection("orders", backend=backend, worker="a")
        await election._try_acquire_or_renew(election._config)

    attempts = metrics_reader.points("grelmicro.leader_election.attempts")
    assert attempts[0][1] == {
        "grelmicro.leader_election.name": "orders",
        "grelmicro.outcome": "acquired",
    }
    leading = metrics_reader.points("grelmicro.leader_election.leading")
    assert leading[0][0] == 1
    assert leading[0][1] == {"grelmicro.leader_election.name": "orders"}


async def test_leader_election_counts_a_standby(
    metrics_reader: MetricsHarness,
) -> None:
    """A worker another replica leads counts unavailable and reads 0."""
    async with MemoryLeaderElectionAdapter() as backend:
        leader = LeaderElection("orders", backend=backend, worker="a")
        standby = LeaderElection("orders", backend=backend, worker="b")
        await leader._try_acquire_or_renew(leader._config)
        await standby._try_acquire_or_renew(standby._config)

    outcomes = {
        str(attrs["grelmicro.outcome"])
        for _, attrs in metrics_reader.points(
            "grelmicro.leader_election.attempts"
        )
    }
    assert outcomes == {"acquired", "unavailable"}


async def test_leader_election_counts_a_backend_error(
    metrics_reader: MetricsHarness,
) -> None:
    """An unreachable backend counts an error, so a dark fleet is visible."""

    class _Failing(MemoryLeaderElectionAdapter):
        async def acquire_or_renew(self, **kwargs: object) -> Any:  # noqa: ANN401
            raise RuntimeError(kwargs)

    async with _Failing() as backend:
        election = LeaderElection("orders", backend=backend, worker="a")
        await election._try_acquire_or_renew(election._config)

    attempts = metrics_reader.points("grelmicro.leader_election.attempts")
    assert attempts[0][1]["grelmicro.outcome"] == "error"
    assert metrics_reader.points("grelmicro.leader_election.leading")[0][0] == 0
