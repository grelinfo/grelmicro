"""Task schedules take whole seconds or a `timedelta`, never a float."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pytest_mock import MockFixture

from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import SettingsValidationError
from grelmicro.task import FireOutcome, TaskRouter
from grelmicro.task._cron import CronTask
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task.samples import count_execution, test1

pytestmark = [pytest.mark.timeout(10)]

UNDER_A_SECOND = timedelta(microseconds=333_333)
"""An interval no float of seconds writes exactly."""

FIRES = 3
"""Iterations the cadence test lets the loop run."""

DUE = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""A fire of an every-minute schedule."""

REFUSED = [
    pytest.param(1.5, id="float"),
    pytest.param(60.0, id="whole-float"),
    pytest.param(True, id="bool"),
    pytest.param("60", id="text"),
]


def _interval_task(router: TaskRouter) -> IntervalTask:
    """Return the one interval task `router` holds."""
    (task,) = router.tasks
    assert isinstance(task, IntervalTask)
    return task


def _cron_task(router: TaskRouter) -> CronTask:
    """Return the one cron task `router` holds."""
    (task,) = router.tasks
    assert isinstance(task, CronTask)
    return task


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(60, timedelta(seconds=60), id="whole-seconds"),
        pytest.param(
            timedelta(milliseconds=500),
            timedelta(milliseconds=500),
            id="under-a-second",
        ),
    ],
)
def test_router_every_interval_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` are taken as the interval."""
    # Arrange
    router = TaskRouter()

    # Act
    router.every(interval=value)(test1)

    # Assert
    assert _interval_task(router)._interval == expected


@pytest.mark.parametrize("value", REFUSED)
def test_router_every_float_bool_or_text_interval_refused(
    value: object,
) -> None:
    """A float, a bool or text is refused, naming `interval`."""
    # Arrange
    router = TaskRouter()

    # Act / Assert
    with pytest.raises(
        ValueError, match="interval must be whole seconds or a timedelta"
    ):
        router.every(interval=value)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_router_every_seconds_keyword_refused() -> None:
    """The `seconds` keyword is gone, with no alias."""
    # Arrange
    router = TaskRouter()

    # Act / Assert
    with pytest.raises(TypeError, match="seconds"):
        router.every(seconds=60)  # type: ignore[call-arg]  # ty: ignore[unknown-argument, missing-argument]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(600, timedelta(seconds=600), id="whole-seconds"),
        pytest.param(
            timedelta(milliseconds=500),
            timedelta(milliseconds=500),
            id="under-a-second",
        ),
    ],
)
def test_router_cron_misfire_grace_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` are taken as the misfire grace."""
    # Arrange
    router = TaskRouter()

    # Act
    router.cron("* * * * *", gate="claim", misfire_grace=value)(test1)

    # Assert
    assert _cron_task(router)._misfire_grace == expected


@pytest.mark.parametrize("value", REFUSED)
def test_router_cron_float_bool_or_text_misfire_grace_refused(
    value: object,
) -> None:
    """A float, a bool or text is refused, naming `misfire_grace`."""
    # Arrange
    router = TaskRouter()

    # Act / Assert
    with pytest.raises(
        ValueError, match="misfire_grace must be whole seconds or a timedelta"
    ):
        router.cron("* * * * *", gate="claim", misfire_grace=value)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_router_cron_misfire_grace_seconds_keyword_refused() -> None:
    """The `misfire_grace_seconds` keyword is gone, with no alias."""
    # Arrange
    router = TaskRouter()

    # Act / Assert
    with pytest.raises(TypeError, match="misfire_grace_seconds"):
        router.cron("* * * * *", misfire_grace_seconds=60)  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


def test_cron_task_without_misfire_grace_holds_none() -> None:
    """No misfire grace sets no budget."""
    # Act
    task = CronTask(expr="* * * * *", function=test1)

    # Assert
    assert task._misfire_grace is None


@pytest.mark.parametrize("value", [0, timedelta(0), -1])
def test_cron_task_misfire_grace_of_zero_or_less_refused(
    value: int | timedelta,
) -> None:
    """A misfire grace of zero or less is refused, naming `misfire_grace`."""
    # Act / Assert
    with pytest.raises(
        ValueError, match="misfire_grace must be greater than zero"
    ):
        CronTask(
            expr="* * * * *",
            function=test1,
            gate="claim",
            misfire_grace=value,
        )


async def test_interval_task_sleeps_exactly_its_interval(
    mocker: MockFixture,
) -> None:
    """Each iteration waits the interval, with no drift between fires."""
    # Arrange
    waits: list[float] = []

    async def record(seconds: float, stop: object) -> bool:
        del stop
        waits.append(seconds)
        return len(waits) == FIRES

    mocker.patch("grelmicro.task._interval.sleep_or_stop", side_effect=record)
    task = IntervalTask(interval=UNDER_A_SECOND, function=count_execution)

    # Act
    await task()

    # Assert
    assert waits == [UNDER_A_SECOND.total_seconds()] * FIRES
    assert samples.execution_count == FIRES


@pytest.mark.parametrize("gate", ["claim", "leader"])
def test_interval_task_claim_lease_is_exactly_two_intervals(
    gate: Literal["claim", "leader"],
) -> None:
    """The claim holds for the interval and leases exactly two of them."""
    # Arrange
    resolved = (
        LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
        if gate == "leader"
        else gate
    )

    # Act
    task = IntervalTask(
        interval=UNDER_A_SECOND,
        function=test1,
        gate=resolved,
    )

    # Assert
    (lock,) = [p for p in task._sync_primitives if isinstance(p, TaskLock)]
    assert lock.config.min_hold_duration == UNDER_A_SECOND
    assert lock.config.lease_duration == timedelta(microseconds=666_666)


def test_interval_task_lock_holding_exactly_the_interval_accepted() -> None:
    """A gate lock that holds exactly a sub-second interval is taken."""
    # Arrange
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=1,
        min_hold_duration=UNDER_A_SECOND,
    )

    # Act
    task = IntervalTask(interval=UNDER_A_SECOND, function=test1, gate=lock)

    # Assert
    assert task._sync_primitives == [lock]


def test_interval_task_lock_one_microsecond_short_refused() -> None:
    """A gate lock that holds one microsecond less than the interval is refused."""
    # Arrange
    lock = TaskLock(
        backend=MemoryLockAdapter(),
        lease_duration=1,
        min_hold_duration=UNDER_A_SECOND - timedelta(microseconds=1),
    )

    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="min_hold_duration must be greater than or equal to interval",
    ):
        IntervalTask(interval=UNDER_A_SECOND, function=test1, gate=lock)


@pytest.mark.parametrize(
    ("grace", "outcome"),
    [
        pytest.param(UNDER_A_SECOND, FireOutcome.SUCCESS, id="at-the-grace"),
        pytest.param(
            UNDER_A_SECOND - timedelta(microseconds=1),
            FireOutcome.MISSED,
            id="one-microsecond-past",
        ),
    ],
)
async def test_cron_task_misfire_grace_boundary_exact_to_the_microsecond(
    grace: timedelta, outcome: FireOutcome, mocker: MockFixture
) -> None:
    """A fire exactly as late as the grace runs, one microsecond later drops."""
    # Arrange
    schedule = MemoryScheduleAdapter()
    await schedule.__aenter__()
    await schedule.claim("grace", (DUE - timedelta(minutes=1)).timestamp())
    now = DUE + UNDER_A_SECOND
    mocker.patch("grelmicro.task._cron._now", side_effect=now.astimezone)
    task = CronTask(
        expr="* * * * *",
        function=count_execution,
        name="grace",
        backend=schedule,
        gate="claim",
        misfire_grace=grace,
    )

    # Act
    await task._tick_guarded(catchup=True)

    # Assert
    assert task.last_fire is not None
    assert task.last_fire.outcome == outcome
