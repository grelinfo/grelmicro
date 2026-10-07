"""Test Interval Task."""

import asyncio
import time
from asyncio import sleep
from datetime import datetime, timedelta
from types import TracebackType
from typing import Self

import pytest
from pytest_mock import MockFixture

from grelmicro._config import reconfigure_all
from grelmicro.coordination._protocol import LockPrimitive
from grelmicro.coordination.errors import LockNotOwnedError, LockReleaseError
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig
from grelmicro.errors import SettingsValidationError
from grelmicro.task import FireInfo, FireOutcome
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
from tests.task.samples import (
    BadLock,
    WouldBlockLock,
    always_fail,
    notify,
    test1,
)


async def sleep_forever() -> None:
    """Block forever on an unset event."""
    await asyncio.Event().wait()


pytestmark = [pytest.mark.timeout(10)]

SLEEP = 0.01
RENEWALS_AT_THE_NEW_PACE = 3
RELOAD_INTERVAL = 60
RELOAD_TUNED = 120
RETRIES_BEFORE_DEADLINE = 2
"""A third of the lease to the first try, then a tenth per retry up to two thirds."""


def test_interval_task_init() -> None:
    """Test Interval Task Initialization."""
    # Act
    task = IntervalTask(seconds=1, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_init_with_name() -> None:
    """Test Interval Task Initialization with Name."""
    # Act
    task = IntervalTask(seconds=1, function=test1, name="test1")
    # Assert
    assert task.name == "test1"


def test_interval_task_init_with_seconds_float() -> None:
    """Test Interval Task accepts a number of seconds."""
    # Arrange
    seconds = 5
    # Act
    task = IntervalTask(seconds=seconds, function=test1)
    # Assert
    assert task._seconds == seconds


def test_interval_task_init_with_seconds_timedelta() -> None:
    """Test Interval Task accepts a timedelta and resolves it to seconds."""
    # Arrange
    interval = timedelta(minutes=2)
    # Act
    task = IntervalTask(seconds=interval, function=test1)
    # Assert
    assert task._seconds == interval.total_seconds()


def test_interval_task_init_with_invalid_interval() -> None:
    """Test Interval Task Initialization with Invalid Interval."""
    # Act / Assert
    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        IntervalTask(seconds=0, function=test1)


def test_interval_task_lock_default_name_restamped() -> None:
    """A default-named lock is re-stamped to the task name."""
    lease_duration = 300
    backend = MemoryLockAdapter()
    task = IntervalTask(
        seconds=60,
        function=test1,
        name="cleanup",
        gate=TaskLock(
            backend=backend,
            lease_duration=timedelta(seconds=lease_duration),
            min_hold_duration=60,
        ),
    )
    task_lock = task._sync_primitives[0]
    assert isinstance(task_lock, TaskLock)
    assert task_lock.name == "cleanup"
    assert task_lock.config.lease_duration == timedelta(seconds=lease_duration)


def test_interval_task_lock_explicit_name_honored() -> None:
    """An explicit-named lock keeps its own name."""
    backend = MemoryLockAdapter()
    task = IntervalTask(
        seconds=60,
        function=test1,
        name="cleanup",
        gate=TaskLock(
            "shared", backend=backend, lease_duration=300, min_hold_duration=60
        ),
    )
    task_lock = task._sync_primitives[0]
    assert isinstance(task_lock, TaskLock)
    assert task_lock.name == "shared"


def test_interval_task_lock_min_hold_less_than_seconds_raises() -> None:
    """A lock min_hold_duration below seconds raises ValueError."""
    backend = MemoryLockAdapter()
    with pytest.raises(
        ValueError,
        match="min_hold_duration must be greater than or equal to seconds",
    ):
        IntervalTask(
            seconds=60,
            function=test1,
            gate=TaskLock(backend=backend, lease_duration=10),
        )


def test_interval_task_leader_auto_locks() -> None:
    """Leader without an explicit lock auto-configures an interval-aware lock."""
    seconds = 60
    leader = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
    task = IntervalTask(
        seconds=seconds,
        function=test1,
        name="cleanup",
        gate=leader,
    )
    task_locks = [p for p in task._sync_primitives if isinstance(p, TaskLock)]
    assert len(task_locks) == 1
    assert task_locks[0].name == "cleanup"
    assert task_locks[0].config.lease_duration == timedelta(seconds=seconds * 2)
    assert task_locks[0].config.min_hold_duration == timedelta(seconds=seconds)


def test_interval_task_local_no_sync() -> None:
    """No gate leaves the task local."""
    task = IntervalTask(seconds=60, function=test1)
    assert task._sync_primitives == []


def test_interval_task_claim_builds_interval_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim holds for the interval, leases two, and ignores the environment."""
    seconds = 60
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_TASKLOCK_LEASE_DURATION", "999")
    monkeypatch.setenv("GREL_TASKLOCK_CLEANUP_MIN_HOLD_DURATION", "1")
    task = IntervalTask(
        seconds=seconds, function=test1, name="cleanup", gate="claim"
    )
    (task_lock,) = task._sync_primitives
    assert isinstance(task_lock, TaskLock)
    assert task_lock.name == "cleanup"
    assert task_lock.config.min_hold_duration == timedelta(seconds=seconds)
    assert task_lock.config.lease_duration == timedelta(seconds=seconds * 2)


def test_interval_task_lock_gate_is_the_callers_handle() -> None:
    """The task enters the lock the caller holds, so its handle stays live."""
    lock = TaskLock(
        backend=MemoryLockAdapter(), lease_duration=120, min_hold_duration=60
    )
    task = IntervalTask(seconds=60, function=test1, name="cleanup", gate=lock)
    assert task._sync_primitives == [lock]
    assert lock.name == "cleanup"


def test_interval_task_lock_gate_refuses_a_second_task() -> None:
    """One `TaskLock` gates one task."""
    lock = TaskLock(
        backend=MemoryLockAdapter(), lease_duration=120, min_hold_duration=60
    )
    IntervalTask(seconds=60, function=test1, name="first", gate=lock)
    with pytest.raises(ValueError, match="already gates task 'first'"):
        IntervalTask(seconds=60, function=test1, name="second", gate=lock)


def test_interval_task_gate_rejects_unknown_value() -> None:
    """A gate outside None, "claim", `TaskLock` and `LeaderElection` is refused."""
    with pytest.raises(TypeError, match="gate must be"):
        IntervalTask(
            seconds=60,
            function=test1,
            gate="leader",  # ty: ignore[invalid-argument-type]
        )


@pytest.mark.parametrize("as_guard", [False, True])
def test_interval_task_sync_rejects_leader_election(*, as_guard: bool) -> None:
    """A leader election passed as `sync` is refused in favour of `gate`."""
    election = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
    sync = election.guard() if as_guard else election
    with pytest.raises(TypeError, match="as gate="):
        IntervalTask(seconds=60, function=test1, sync=sync)


@pytest.mark.parametrize(
    ("gate", "label"),
    [
        (None, "none"),
        ("claim", "claim"),
        ("lock", "TaskLock('named')"),
        ("leader", "LeaderElection('svc')"),
    ],
)
async def test_interval_task_logs_gate_at_start(
    mocker: MockFixture,
    caplog: pytest.LogCaptureFixture,
    gate: str | None,
    label: str,
) -> None:
    """The start log names the resolved gate."""
    caplog.set_level("INFO")
    resolved = {
        "lock": TaskLock("named", lease_duration=60, min_hold_duration=60),
        "leader": LeaderElection("svc", backend=MemoryLeaderElectionAdapter()),
    }.get(gate or "", gate)
    task = IntervalTask(
        seconds=60,
        function=test1,
        gate=resolved,  # ty: ignore[invalid-argument-type]
    )
    mocker.patch.object(task, "_run_with_sync")

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        cancel_group(tg)

    assert any(f"gate: {label})" in r.message for r in caplog.records)


async def test_interval_task_start() -> None:
    """Test Interval Task Start."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)


async def test_interval_task_last_fire_outcome() -> None:
    """last_fire.outcome is the FireOutcome.SUCCESS member after a run."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.last_fire is not None
        assert isinstance(task.last_fire, FireInfo)
        assert task.last_fire.outcome is FireOutcome.SUCCESS
        assert isinstance(task.last_fire.outcome, FireOutcome)
        assert task.last_fire.outcome == "success"
        cancel_group(tg)


async def test_interval_task_execution_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Execution Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=always_fail)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task execution error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_would_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task WouldBlock logs at DEBUG, not ERROR."""
    # Arrange
    caplog.set_level("DEBUG")
    task = IntervalTask(seconds=1, function=notify, sync=WouldBlockLock())

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task skipped:" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
    assert not any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_synchronization_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Synchronization Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify, sync=BadLock())

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_stop(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Interval Task stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._interval.asyncio.sleep",
        side_effect=CustomBaseException,
    )
    task = IntervalTask(seconds=1, function=test1)

    async def leader_election_during_runtime_error() -> None:
        async with asyncio.TaskGroup() as tg:
            await start_task(tg, task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await leader_election_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


# --- Introspection ---


def test_interval_task_next_fire_time_none_before_start() -> None:
    """next_fire_time is None before the loop starts."""
    task = IntervalTask(seconds=1, function=test1)
    assert task.next_fire_time is None


def test_interval_task_last_fire_none_before_start() -> None:
    """last_fire is None before the first fire."""
    task = IntervalTask(seconds=1, function=test1)
    assert task.last_fire is None


async def test_interval_task_next_fire_time_after_loop_starts() -> None:
    """next_fire_time is a timezone-aware datetime after the loop starts running."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        # Give the loop one tick so _last_loop_start is recorded.
        await sleep(SLEEP)
        cancel_group(tg)
    nft = task.next_fire_time
    assert nft is not None
    assert isinstance(nft, datetime)
    assert nft.tzinfo is not None


async def test_interval_task_last_fire_success() -> None:
    """last_fire.outcome is 'success' after a successful run."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.last_fire is not None
        assert isinstance(task.last_fire, FireInfo)
        assert task.last_fire.outcome == "success"
        assert task.last_fire.duration >= 0
        cancel_group(tg)


async def test_interval_task_last_fire_error() -> None:
    """last_fire.outcome is 'error' after a failed run."""
    task = IntervalTask(seconds=1, function=always_fail)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "error"
        cancel_group(tg)


async def test_interval_task_last_fire_skipped() -> None:
    """last_fire.outcome is 'skipped' when WouldBlockError is raised."""
    task = IntervalTask(seconds=1, function=notify, sync=WouldBlockLock())
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "skipped"
        assert task.last_fire.duration == 0.0
        cancel_group(tg)


async def test_interval_task_lock_gate_refuses_a_shorter_reconfigured_hold() -> (
    None
):
    """A gate lock reconfigured to hold less than the interval is refused."""
    interval = 60
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    IntervalTask(seconds=interval, function=test1, name="cleanup", gate=lock)
    shorter = lock.config.model_copy(
        update={"min_hold_duration": timedelta(seconds=1)}
    )

    with pytest.raises(SettingsValidationError, match="min_hold_duration"):
        await lock.reconfigure(shorter)

    assert lock.config.min_hold_duration == timedelta(seconds=interval)


async def test_interval_task_lock_gate_takes_a_longer_reconfigured_hold() -> (
    None
):
    """A gate lock reconfigured to hold longer than the interval is taken."""
    interval = 60
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 5),
        min_hold_duration=timedelta(seconds=interval),
    )
    IntervalTask(seconds=interval, function=test1, name="cleanup", gate=lock)
    longer = lock.config.model_copy(
        update={"min_hold_duration": timedelta(seconds=interval * 2)}
    )

    await lock.reconfigure(longer)

    assert lock.config.min_hold_duration == timedelta(seconds=interval * 2)


@pytest.mark.parametrize("gate", ["claim", "leader"])
async def test_interval_task_built_claim_lock_refuses_a_shorter_hold(
    gate: str,
) -> None:
    """The lock a claim or leader gate builds keeps holding for the interval."""
    interval = 60
    resolved = (
        LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
        if gate == "leader"
        else gate
    )
    task = IntervalTask(
        seconds=interval,
        function=test1,
        gate=resolved,  # ty: ignore[invalid-argument-type]
    )
    (lock,) = [p for p in task._sync_primitives if isinstance(p, TaskLock)]
    shorter = lock.config.model_copy(
        update={"min_hold_duration": timedelta(seconds=1)}
    )

    with pytest.raises(SettingsValidationError, match="min_hold_duration"):
        await lock.reconfigure(shorter)

    assert lock.config.min_hold_duration == timedelta(seconds=interval)


async def test_interval_task_renewal_stops_when_the_body_ends(
    mocker: MockFixture,
) -> None:
    """The claim is renewed while the body runs, and never after it ends."""
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = interval * 4
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    refresh = mocker.spy(lock, "_renew_held")

    await task._run_with_sync(task._sync_primitives)
    renewals = refresh.call_count
    await sleep(interval * 2)

    assert renewals >= 1
    assert refresh.call_count == renewals


async def test_interval_task_lost_claim_warns_once_and_keeps_the_body(
    mocker: MockFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim the backend no longer holds warns once, and the body finishes."""
    caplog.set_level("WARNING")
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = interval * 4
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    refresh = mocker.patch.object(
        lock, "_renew_held", side_effect=LockNotOwnedError(name="lost")
    )

    with pytest.raises(LockNotOwnedError):
        await task._run_with_sync(task._sync_primitives)

    assert samples.e2e_event_1.is_set()
    assert refresh.call_count == 1
    lost = [r for r in caplog.records if "lost its claim" in r.message]
    assert len(lost) == 1


async def test_interval_task_renewal_retries_until_the_deadline(
    mocker: MockFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable backend is retried, then given up at the deadline."""
    caplog.set_level("WARNING")
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = interval * 6
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    refresh = mocker.patch.object(
        lock, "_renew_held", side_effect=LockReleaseError(name="down")
    )

    with pytest.raises(LockNotOwnedError):
        await task._run_with_sync(task._sync_primitives)

    assert refresh.call_count >= RETRIES_BEFORE_DEADLINE
    lost = [r for r in caplog.records if "could not renew" in r.message]
    assert len(lost) == 1


async def test_interval_task_logs_a_claim_lost_before_release(
    mocker: MockFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim lost while the body ran is logged, and the loop goes on."""
    caplog.set_level("WARNING")
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = interval * 4
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    mocker.patch.object(
        lock, "_renew_held", side_effect=LockNotOwnedError(name="lost")
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        await sleep(interval)
        cancel_group(tg)

    assert any("no longer held" in r.message for r in caplog.records)


async def test_interval_task_cancel_survives_a_claim_lost_on_release(
    mocker: MockFixture,
) -> None:
    """Cancelling mid-body still stops the task when the release then fails."""
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    task = IntervalTask(
        seconds=interval, function=samples.worker_1_hold, gate=lock
    )
    mocker.patch.object(
        lock, "_renew_held", side_effect=LockNotOwnedError(name="lost")
    )

    async with asyncio.TaskGroup() as tg:
        handle = await start_task(tg, task)
        await samples.e2e_event_1.wait()
        await sleep(interval * 3)
        cancel_group(tg)

    assert handle.cancelled()


async def test_interval_task_default_named_gate_reloads_under_its_task() -> (
    None
):
    """A default-named gate lock takes external config under its task name."""
    first, second = (
        TaskLock(
            lease_duration=timedelta(seconds=RELOAD_INTERVAL * 10),
            min_hold_duration=timedelta(seconds=RELOAD_INTERVAL),
            env_load=False,
        )
        for _ in range(2)
    )
    IntervalTask(
        seconds=RELOAD_INTERVAL, function=test1, name="first", gate=first
    )
    IntervalTask(
        seconds=RELOAD_INTERVAL, function=test1, name="second", gate=second
    )

    await reconfigure_all(
        {
            "GREL_TASKLOCK_FIRST_MIN_HOLD_DURATION": str(RELOAD_TUNED),
            "GREL_TASKLOCK_MIN_HOLD_DURATION": str(RELOAD_INTERVAL * 5),
        }
    )

    assert first.config.min_hold_duration == timedelta(seconds=RELOAD_TUNED)
    assert second.config.min_hold_duration == timedelta(seconds=RELOAD_INTERVAL)


async def test_interval_task_gate_built_from_config_stays_static() -> None:
    """A default-named gate lock built from a config ignores external reload."""
    lock = TaskLock.from_config(
        "default",
        TaskLockConfig(
            worker="worker",
            lease_duration=timedelta(seconds=RELOAD_INTERVAL * 10),
            min_hold_duration=timedelta(seconds=RELOAD_INTERVAL),
        ),
        backend=MemoryLockAdapter(),
    )
    IntervalTask(
        seconds=RELOAD_INTERVAL, function=test1, name="static", gate=lock
    )

    await reconfigure_all(
        {"GREL_TASKLOCK_STATIC_MIN_HOLD_DURATION": str(RELOAD_TUNED)}
    )

    assert lock.config.min_hold_duration == timedelta(seconds=RELOAD_INTERVAL)


class _SlowLock(LockPrimitive):
    """Resource lock that takes a while to enter, as a contended one does."""

    def __init__(self, delay: float) -> None:
        """Wait `delay` seconds on every entry."""
        self._delay = delay

    async def __aenter__(self) -> Self:
        """Enter after the delay."""
        await sleep(self._delay)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit at once."""


async def test_interval_task_renews_the_claim_while_waiting_for_sync() -> None:
    """The claim is renewed from the moment it is held, sync wait included."""
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 2),
        min_hold_duration=timedelta(seconds=interval),
    )
    task = IntervalTask(
        seconds=interval,
        function=test1,
        gate=lock,
        sync=_SlowLock(interval * 4),
    )

    await task._run_with_sync(task._sync_primitives)

    assert task.last_fire is not None
    assert task.last_fire.outcome == FireOutcome.SUCCESS


async def test_interval_task_renewal_follows_a_reconfigured_lease(
    mocker: MockFixture,
) -> None:
    """A lease shortened while the body runs is renewed at its new pace."""
    interval = 0.03
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=interval * 20),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = interval * 10
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    renewals = mocker.spy(lock, "_renew_held")
    shorter = lock.config.model_copy(
        update={"lease_duration": timedelta(seconds=interval * 3)}
    )

    async def shorten() -> None:
        await sleep(interval)
        await lock.reconfigure(shorter)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(shorten())
        await task._run_with_sync(task._sync_primitives)

    assert renewals.call_count >= RENEWALS_AT_THE_NEW_PACE


async def test_interval_task_renewal_outlasts_a_short_backend_outage(
    mocker: MockFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Renewal keeps retrying while the lease lasts, so a short outage passes."""
    caplog.set_level("WARNING")
    interval = 0.03
    lease = interval * 4
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=timedelta(seconds=lease),
        min_hold_duration=timedelta(seconds=interval),
    )
    samples.long_body_seconds = lease * 1.5
    task = IntervalTask(
        seconds=interval, function=samples.run_long_body, gate=lock
    )
    renew = lock._renew_held
    back_at = time.monotonic() + lease * 0.85

    async def down_then_back() -> None:
        if time.monotonic() < back_at:
            raise LockReleaseError(name="down")
        await renew()

    mocker.patch.object(lock, "_renew_held", side_effect=down_then_back)

    await task._run_with_sync(task._sync_primitives)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_interval_task_default_gate_on_a_name_env_cannot_spell() -> None:
    """A task name no env var can spell still binds, and reload skips it."""
    lock = TaskLock(
        lease_duration=timedelta(seconds=RELOAD_INTERVAL * 2),
        min_hold_duration=timedelta(seconds=RELOAD_INTERVAL),
        env_load=False,
    )
    IntervalTask(
        seconds=RELOAD_INTERVAL, function=test1, name="5m-sync", gate=lock
    )

    await reconfigure_all(
        {"GREL_TASKLOCK_MIN_HOLD_DURATION": str(RELOAD_TUNED)}
    )

    assert lock.name == "5m-sync"
    assert lock.config.min_hold_duration == timedelta(seconds=RELOAD_INTERVAL)
