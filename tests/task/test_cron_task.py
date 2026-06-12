"""Test Cron Task (durable design)."""

import asyncio
from asyncio import sleep
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
from pytest_mock import MockFixture

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.abc import LockPrimitive
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.memory import MemoryScheduleAdapter
from grelmicro.task._cron import CronTask, FireInfo
from grelmicro.task.errors import CronError
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
from tests.task.samples import (
    WouldBlockLock,
    always_fail,
    count_execution,
    notify,
    test1,
)


class OkLock(LockPrimitive):
    """Lock that enters and exits cleanly, recording its use."""

    def __init__(self) -> None:
        """Track how many times the lock body ran."""
        self.entered = 0

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the synchronization primitive."""


class LockLostLock(LockPrimitive):
    """Lock that raises `LockNotOwnedError` on enter."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        raise LockNotOwnedError(name="cron")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the synchronization primitive."""


async def sleep_forever() -> None:
    """Block forever on an unset event."""
    await asyncio.Event().wait()


pytestmark = [pytest.mark.timeout(10)]

# A cron expression that fires every minute, used so the task body runs
# quickly when its first sleep is patched to return immediately.
EVERY_MINUTE = "* * * * *"
SLEEP = 0.01


def test_cron_task_init() -> None:
    """Test Cron Task Initialization."""
    # Act
    task = CronTask(expr=EVERY_MINUTE, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_cron_task_init_with_name() -> None:
    """Test Cron Task Initialization with Name."""
    # Act
    task = CronTask(expr="0 2 * * *", function=test1, name="nightly")
    # Assert
    assert task.name == "nightly"


def test_cron_task_init_with_timezone() -> None:
    """Test Cron Task accepts a timezone."""
    # Act
    task = CronTask(
        expr="0 2 * * *",
        function=test1,
        timezone="Europe/Zurich",
        name="zurich",
    )
    # Assert
    assert task.name == "zurich"


def test_cron_task_init_invalid_expression() -> None:
    """Test Cron Task Initialization with invalid expression."""
    # Act / Assert
    with pytest.raises(CronError):
        CronTask(expr="not a cron", function=test1)


def test_cron_task_init_invalid_timezone() -> None:
    """Test Cron Task Initialization with invalid timezone."""
    # Act / Assert
    with pytest.raises(Exception):  # noqa: B017, PT011
        CronTask(expr=EVERY_MINUTE, function=test1, timezone="Mars/Phobos")


def _run_fast(mocker: MockFixture) -> None:
    """Patch the loop sleep so every iteration runs almost immediately.

    Keeps a tiny real sleep so the loop yields to the event loop and never
    starves the awaiting test coroutine. Also pins the cron wall clock to a
    fixed instant so the spinning loop never straddles a real minute boundary,
    which would otherwise add a second, legitimate fire and flake the count.
    """

    async def fast_sleep(seconds: float, stop: object) -> bool:
        del seconds, stop
        await asyncio.sleep(SLEEP)
        return False

    mocker.patch(
        "grelmicro.task._cron.sleep_or_stop",
        side_effect=fast_sleep,
    )
    frozen = datetime.now(UTC).replace(second=30, microsecond=0)
    mocker.patch(
        "grelmicro.task._cron._now",
        side_effect=frozen.astimezone,
    )


async def test_cron_task_start(mocker: MockFixture) -> None:
    """Test Cron Task runs the body locally without a backend."""
    # Arrange
    task = CronTask(expr=EVERY_MINUTE, function=notify)
    _run_fast(mocker)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)


async def test_cron_task_execution_error(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Cron Task Execution Error is caught and logged."""
    # Arrange
    task = CronTask(expr=EVERY_MINUTE, function=always_fail)
    _run_fast(mocker)
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


async def test_cron_stop(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Cron Task stop logs cleanly."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._cron.sleep_or_stop",
        side_effect=CustomBaseException,
    )
    task = CronTask(expr=EVERY_MINUTE, function=test1)

    async def cron_during_runtime_error() -> None:
        async with asyncio.TaskGroup() as tg:
            await start_task(tg, task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await cron_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


# --- Durable schedule backend ---


@pytest.fixture
async def schedule() -> MemoryScheduleAdapter:
    """Return an opened in-memory schedule backend."""
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    return backend


def _previous_fire_epoch() -> float:
    """Return the epoch of the most recent whole-minute fire at or before now."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now.timestamp()


async def test_cron_task_runs_once_with_backend(
    schedule: MemoryScheduleAdapter, mocker: MockFixture
) -> None:
    """A normal fire runs exactly once with a schedule backend.

    The backend is pre-seeded with a baseline below the current fire so the
    current fire counts as a new fire to claim (not a first-sight baseline).
    """
    # Arrange
    name = "runs-once"
    await schedule.claim(name, _previous_fire_epoch() - 120)
    task = CronTask(
        expr=EVERY_MINUTE,
        function=count_execution,
        name=name,
        backend=schedule,
    )
    _run_fast(mocker)
    # Act: loop fast over many ticks against a single due fire.
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 5)
        cancel_group(tg)
    # Assert: the due fire ran exactly once and the backend advanced.
    assert samples.execution_count == 1
    assert await schedule.last_fired(name) is not None


async def test_cron_task_second_worker_does_not_double_run(
    schedule: MemoryScheduleAdapter, mocker: MockFixture
) -> None:
    """Two workers sharing a backend run the same fire only once."""
    # Arrange
    name = "no-double-run"
    await schedule.claim(name, _previous_fire_epoch() - 120)
    worker_a = CronTask(
        expr=EVERY_MINUTE, function=count_execution, name=name, backend=schedule
    )
    worker_b = CronTask(
        expr=EVERY_MINUTE, function=count_execution, name=name, backend=schedule
    )
    _run_fast(mocker)
    # Act: both workers loop fast against the same shared fire.
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, worker_a)
        await start_task(tg, worker_b)
        await sleep(SLEEP * 5)
        cancel_group(tg)
    # Assert: only one worker claimed and ran the single due fire.
    assert samples.execution_count == 1


async def test_cron_task_replays_missed_fire(
    schedule: MemoryScheduleAdapter, mocker: MockFixture
) -> None:
    """A fire missed while down replays once on restart."""
    # Arrange: last_fired sits two minutes in the past, so the current fire
    # was missed and is due for replay.
    name = "missed-replay"
    await schedule.claim(name, _previous_fire_epoch() - 120)
    task = CronTask(
        expr=EVERY_MINUTE, function=count_execution, name=name, backend=schedule
    )
    _run_fast(mocker)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 5)
        cancel_group(tg)
    # Assert: the missed fire ran exactly once (coalesced, no backlog).
    assert samples.execution_count == 1


async def test_cron_task_misfire_grace_skips_when_too_late(
    schedule: MemoryScheduleAdapter, mocker: MockFixture
) -> None:
    """A missed fire past the grace budget is skipped, not replayed."""
    # Arrange: the due fire is ~minutes old, well past a 1-second grace.
    name = "grace-skip"
    await schedule.claim(name, _previous_fire_epoch() - 120)
    task = CronTask(
        expr=EVERY_MINUTE,
        function=count_execution,
        name=name,
        backend=schedule,
        misfire_grace_seconds=1,
    )
    _run_fast(mocker)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 5)
        cancel_group(tg)
    # Assert: never ran, but the baseline advanced so it is not retried.
    assert samples.execution_count == 0
    assert await schedule.last_fired(name) == pytest.approx(
        _previous_fire_epoch(), abs=60
    )


async def test_cron_task_first_sight_establishes_baseline(
    schedule: MemoryScheduleAdapter, mocker: MockFixture
) -> None:
    """A brand-new schedule seeds the baseline without running."""
    # Arrange: no prior last_fired for this name.
    name = "first-sight"
    task = CronTask(
        expr=EVERY_MINUTE, function=count_execution, name=name, backend=schedule
    )
    _run_fast(mocker)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 5)
        cancel_group(tg)
    # Assert: the first tick only set the baseline, the body never ran.
    assert samples.execution_count == 0
    assert await schedule.last_fired(name) is not None


# --- Introspection ---


def test_cron_task_next_fire_time_none_before_start() -> None:
    """next_fire_time is None before the loop starts."""
    task = CronTask(expr=EVERY_MINUTE, function=test1)
    assert task.next_fire_time is None


def test_cron_task_last_fire_none_before_start() -> None:
    """last_fire is None before the first fire."""
    task = CronTask(expr=EVERY_MINUTE, function=test1)
    assert task.last_fire is None


async def test_cron_task_next_fire_time_set_after_loop_starts(
    mocker: MockFixture,
) -> None:
    """next_fire_time is populated once the loop begins sleeping."""
    task = CronTask(expr=EVERY_MINUTE, function=notify)
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.next_fire_time is not None
        assert isinstance(task.next_fire_time, datetime)
        cancel_group(tg)


async def test_cron_task_last_fire_success(mocker: MockFixture) -> None:
    """last_fire.outcome is 'success' after a successful run."""
    task = CronTask(expr=EVERY_MINUTE, function=notify)
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.last_fire is not None
        assert task.last_fire.outcome == "success"
        assert isinstance(task.last_fire, FireInfo)
        assert task.last_fire.duration >= 0
        cancel_group(tg)


async def test_cron_task_last_fire_error(mocker: MockFixture) -> None:
    """last_fire.outcome is 'error' after a failed run."""
    task = CronTask(expr=EVERY_MINUTE, function=always_fail)
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 3)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "error"
        cancel_group(tg)


async def test_cron_task_last_fire_skipped(mocker: MockFixture) -> None:
    """last_fire.outcome is 'skipped' when WouldBlockError is raised."""
    task = CronTask(expr=EVERY_MINUTE, function=notify, sync=WouldBlockLock())
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 3)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "skipped"
        assert task.last_fire.duration == 0.0
        cancel_group(tg)


# --- Synchronization and tick error handling ---


async def test_cron_task_runs_body_under_sync_lock(mocker: MockFixture) -> None:
    """A passing `sync` lock wraps the body and the body still runs."""
    lock = OkLock()
    task = CronTask(expr=EVERY_MINUTE, function=notify, sync=lock)
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)
    assert lock.entered >= 1


async def test_cron_task_lock_lost_is_logged(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """A `LockNotOwnedError` from the sync lock is caught and warned."""
    caplog.set_level("WARNING")
    task = CronTask(expr=EVERY_MINUTE, function=notify, sync=LockLostLock())
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 3)
        cancel_group(tg)
    assert any(
        "lock expired" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


async def test_cron_task_tick_error_is_logged(
    schedule: MemoryScheduleAdapter,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """A non-cron error raised inside a tick is caught and logged."""
    name = "tick-error"
    await schedule.claim(name, _previous_fire_epoch() - 120)
    task = CronTask(
        expr=EVERY_MINUTE, function=count_execution, name=name, backend=schedule
    )
    mocker.patch.object(
        schedule, "claim", side_effect=RuntimeError("backend down")
    )
    _run_fast(mocker)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP * 3)
        cancel_group(tg)
    assert any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_cron_task_stop_event_breaks_loop(mocker: MockFixture) -> None:
    """`sleep_or_stop` returning True breaks the loop without another tick."""

    async def stop_now(seconds: float, stop: object) -> bool:
        del seconds, stop
        return True

    mocker.patch("grelmicro.task._cron.sleep_or_stop", side_effect=stop_now)
    task = CronTask(expr=EVERY_MINUTE, function=count_execution)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)


async def test_tick_guarded_propagates_cancellation(
    mocker: MockFixture,
) -> None:
    """A `CancelledError` raised inside a tick propagates out of the guard."""
    task = CronTask(expr=EVERY_MINUTE, function=count_execution)
    mocker.patch.object(task, "_tick", side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await task._tick_guarded(catchup=False)


async def test_tick_guarded_reraises_shadowed_cancellation(
    mocker: MockFixture,
) -> None:
    """A pending cancellation shadowed by a regular error is re-raised."""
    task = CronTask(expr=EVERY_MINUTE, function=count_execution)

    async def cancel_then_fail(*, catchup: bool) -> None:
        # Request cancellation mid-tick, then raise a regular error that the
        # guard swallows. The pending cancellation must surface afterwards.
        del catchup
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        msg = "shadows the cancellation"
        raise RuntimeError(msg)

    mocker.patch.object(task, "_tick", side_effect=cancel_then_fail)

    runner = asyncio.create_task(task._tick_guarded(catchup=False))
    with pytest.raises(asyncio.CancelledError):
        await runner


async def test_cron_task_resolves_backend_from_active_app() -> None:
    """Without an explicit backend, the task resolves it from the app."""
    schedule = MemoryScheduleAdapter()
    micro = Grelmicro(uses=[Coordination(schedule=schedule)])
    task = CronTask(expr=EVERY_MINUTE, function=count_execution)

    async with micro:
        assert task.backend is schedule
