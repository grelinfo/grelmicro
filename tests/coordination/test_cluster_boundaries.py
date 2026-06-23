"""Boundary tests for coordination clusters left by the earlier campaign.

These pin the memory fencing-token counter, the leader `last_confirmation_age`
sign, and the lock-name length boundary, so a flipped operator or off-by-one
in those spots is caught.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

import grelmicro.coordination.leaderelection as le_module
import grelmicro.coordination.memory as mem_module
import grelmicro.coordination.tasklock as tl_module
from grelmicro.coordination.abc import LeaderElectionBackend, LeaderRecord
from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)
from grelmicro.coordination.lock import Lock, LockConfig
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType
    from typing import Self

_NAME_MAX_LEN = 200
_DURATION = 60.0
_CONFIRMED_AT = 100.0
_NOW = 105.0
_SECOND_TOKEN = 2
_MIN_LOCK = 10.0
_ELAPSED = 2.0
_LEASE = 10.0
_RENEW_DELTA = 3.0
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _freeze_memory_datetime(
    monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """Pin `datetime.now(UTC)` inside the memory module to `now`."""

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # noqa: ARG003
            return now

    monkeypatch.setattr(mem_module, "datetime", _FixedDatetime)


async def test_fence_token_starts_at_one_and_climbs() -> None:
    """The fencing token starts at 1 and increments on each free-to-held."""
    async with MemoryLockAdapter() as backend:
        first = await backend.acquire(name="x", token="a", duration=_DURATION)
        assert first == 1

        await backend.release(name="x", token="a")
        second = await backend.acquire(name="x", token="b", duration=_DURATION)
        assert second == _SECOND_TOKEN


def test_last_confirmation_age_is_now_minus_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`last_confirmation_age` is `now - last_confirmed`, not their sum."""
    election = LeaderElection("le-age")
    election._last_confirmed_at = _CONFIRMED_AT
    monkeypatch.setattr(le_module, "monotonic", lambda: _NOW)

    assert election.last_confirmation_age() == _NOW - _CONFIRMED_AT


def test_lock_name_at_max_length_is_valid() -> None:
    """A name of exactly the maximum length is valid; one over is rejected."""
    Lock("a" * _NAME_MAX_LEN)  # no raise at the boundary

    with pytest.raises(ValueError, match="at most"):
        Lock("a" * (_NAME_MAX_LEN + 1))


async def test_lock_is_still_held_at_the_exact_expiry_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `now == expire_at` the lock is still held (the guard is `>=`)."""
    clock = {"t": _CONFIRMED_AT}
    monkeypatch.setattr(mem_module, "monotonic", lambda: clock["t"])

    async with MemoryLockAdapter() as backend:
        await backend.acquire(name="x", token="a", duration=_DURATION)
        clock["t"] = _CONFIRMED_AT + _DURATION  # exactly the expiry instant

        assert await backend.locked(name="x") is True
        assert await backend.owned(name="x", token="a") is True
        assert await backend.release(name="x", token="a") is True


async def test_do_exit_reacquires_with_remaining_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-acquire uses `min_lock - elapsed`, not `min_lock + elapsed`."""
    task_lock = TaskLock("exit-remaining")
    task_lock._acquired_at = _CONFIRMED_AT
    monkeypatch.setattr(
        tl_module, "monotonic", lambda: _CONFIRMED_AT + _ELAPSED
    )
    captured: dict[str, float] = {}

    async def fake_reacquire(_token: str, duration: float) -> bool:
        captured["duration"] = duration
        return True

    monkeypatch.setattr(task_lock, "do_reacquire", fake_reacquire)

    await task_lock.do_exit("tok", min_lock_seconds=_MIN_LOCK)

    assert captured["duration"] == _MIN_LOCK - _ELAPSED


async def test_do_exit_releases_at_exact_min_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `elapsed == min_lock` the lock is released (the guard is `>=`)."""
    task_lock = TaskLock("exit-boundary")
    task_lock._acquired_at = _CONFIRMED_AT
    monkeypatch.setattr(
        tl_module, "monotonic", lambda: _CONFIRMED_AT + _MIN_LOCK
    )
    calls: dict[str, bool] = {"released": False, "reacquired": False}

    async def fake_release(_token: str) -> bool:
        calls["released"] = True
        return True

    async def fake_reacquire(_token: str, _duration: float) -> bool:
        calls["reacquired"] = True
        return True

    monkeypatch.setattr(task_lock, "do_release", fake_release)
    monkeypatch.setattr(task_lock, "do_reacquire", fake_reacquire)

    await task_lock.do_exit("tok", min_lock_seconds=_MIN_LOCK)

    assert calls["released"] is True
    assert calls["reacquired"] is False


async def test_acquire_blocks_other_holder_at_exact_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `now == expire_at` a different token cannot take over (guard is `<`)."""
    clock = {"t": _CONFIRMED_AT}
    monkeypatch.setattr(mem_module, "monotonic", lambda: clock["t"])

    async with MemoryLockAdapter() as backend:
        assert (
            await backend.acquire(name="x", token="a", duration=_DURATION) == 1
        )
        clock["t"] = _CONFIRMED_AT + _DURATION  # exactly the expiry instant

        assert (
            await backend.acquire(name="x", token="b", duration=_DURATION)
            is None
        )


async def test_live_record_expires_at_exact_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `now == renewed_at + lease` the record is expired (guard is `>=`)."""
    backend = MemoryLeaderElectionAdapter()
    _freeze_memory_datetime(monkeypatch, _EPOCH)
    await backend.acquire_or_renew(name="svc", token="w1", duration=_LEASE)

    _freeze_memory_datetime(monkeypatch, _EPOCH + timedelta(seconds=_LEASE))

    assert await backend.get(name="svc") is None


async def test_renew_forwards_renewed_at_and_lease_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renewing the live lease writes the new renewed_at and lease_duration."""
    backend = MemoryLeaderElectionAdapter()
    _freeze_memory_datetime(monkeypatch, _EPOCH)
    first = await backend.acquire_or_renew(
        name="svc", token="w1", duration=_LEASE
    )

    renewed_now = _EPOCH + timedelta(seconds=_RENEW_DELTA)
    _freeze_memory_datetime(monkeypatch, renewed_now)
    second = await backend.acquire_or_renew(
        name="svc", token="w1", duration=_DURATION
    )

    assert second.renewed_at == renewed_now
    assert second.lease_duration == _DURATION
    assert second.acquired_at == first.acquired_at


_META = {"pod": "p-1", "region": "eu"}
_SLOW_BACKEND_TIMEOUT = 0.02
_SLOW_BACKEND_SLEEP = 0.2


def _record(token: str) -> LeaderRecord:
    """Build a live `LeaderRecord` held by `token`."""
    now = datetime.now(UTC)
    return LeaderRecord(
        holder=token,
        lease_duration=_LEASE,
        acquired_at=now,
        renewed_at=now,
        transitions=0,
        metadata={},
    )


class _RecordingBackend(LeaderElectionBackend):
    """Backend double that captures the metadata passed to acquire_or_renew."""

    def __init__(self) -> None:
        self.metadata_seen: Mapping[str, str] | None | str = "unset"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire_or_renew(
        self,
        *,
        name: str,  # noqa: ARG002
        token: str,
        duration: float,  # noqa: ARG002
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        self.metadata_seen = metadata
        return _record(token)

    async def release(self, *, name: str, token: str) -> bool:  # noqa: ARG002
        return True

    async def get(self, *, name: str) -> LeaderRecord | None:  # noqa: ARG002
        return None


class _SlowBackend(LeaderElectionBackend):
    """Backend double whose calls outlast the configured backend_timeout."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire_or_renew(
        self,
        *,
        name: str,  # noqa: ARG002
        token: str,
        duration: float,  # noqa: ARG002
        metadata: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> LeaderRecord:
        await asyncio.sleep(_SLOW_BACKEND_SLEEP)
        return _record(token)

    async def release(self, *, name: str, token: str) -> bool:  # noqa: ARG002
        await asyncio.sleep(_SLOW_BACKEND_SLEEP)
        return True

    async def get(self, *, name: str) -> LeaderRecord | None:  # noqa: ARG002
        return None


def _slow_config(worker: str) -> LeaderElectionConfig:
    """Config with a tiny backend_timeout shorter than the slow backend sleep."""
    return LeaderElectionConfig(
        worker=worker,
        lease_duration=10,
        renew_deadline=5,
        retry_interval=0.5,
        backend_timeout=_SLOW_BACKEND_TIMEOUT,
        error_interval=30,
    )


async def test_init_forwards_metadata_to_backend() -> None:
    """Metadata passed to the constructor reaches the backend acquire call."""
    backend = _RecordingBackend()
    election = LeaderElection("le-meta", backend=backend, metadata=_META)

    await election._try_acquire_or_renew(election._config)

    assert backend.metadata_seen == _META


async def test_from_config_forwards_metadata_to_backend() -> None:
    """Metadata passed to from_config reaches the backend acquire call."""
    backend = _RecordingBackend()
    election = LeaderElection.from_config(
        "le-meta-fc",
        LeaderElectionConfig(worker="w1"),
        backend=backend,
        metadata=_META,
    )

    await election._try_acquire_or_renew(election._config)

    assert backend.metadata_seen == _META


async def test_record_is_none_before_first_attempt() -> None:
    """A freshly built election exposes `record is None`."""
    election = LeaderElection("le-record", backend=_RecordingBackend())

    assert election.record is None


async def test_is_leader_confirmed_within_requires_a_confirmation() -> None:
    """Leader with no confirmation timestamp fails the freshness check."""
    election = LeaderElection("le-confirm", backend=_RecordingBackend())
    election._is_leader = True
    election._last_confirmed_at = None

    assert election.is_leader_confirmed_within(10.0) is False


async def test_acquire_times_out_on_a_slow_backend() -> None:
    """A backend slower than backend_timeout is abandoned, leaving no record."""
    election = LeaderElection.from_config(
        "le-timeout", _slow_config("w1"), backend=_SlowBackend()
    )

    await election._try_acquire_or_renew(election._config)

    assert election.record is None
    assert election.is_leader() is False


async def test_release_times_out_on_a_slow_backend() -> None:
    """Release wrapped in backend_timeout returns without hanging on a slow backend."""
    election = LeaderElection.from_config(
        "le-rel-timeout", _slow_config("w1"), backend=_SlowBackend()
    )

    async with asyncio.timeout(_SLOW_BACKEND_SLEEP / 2):
        await election._release()


async def test_tasklock_from_config_wires_backend() -> None:
    """from_config keeps the explicit backend so acquire reaches it."""
    config = TaskLockConfig(
        worker="w1", min_lock_seconds=1, max_lock_seconds=10
    )
    async with MemoryLockAdapter() as backend:
        task_lock = TaskLock.from_config(
            "tl-from-config", config, backend=backend
        )

        async with task_lock:
            assert await backend.locked(name="tasklock:tl-from-config") is True


async def test_lock_from_config_wires_backend() -> None:
    """from_config keeps the explicit backend so acquire reaches it."""
    config = LockConfig(worker="w1", lease_duration=60)
    async with MemoryLockAdapter() as backend:
        lock = Lock.from_config("lk-from-config", config, backend=backend)

        await lock.acquire()

        assert await backend.locked(name="lock:lk-from-config") is True
