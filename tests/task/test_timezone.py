"""Test the task timezone: inheritance, resolution, and DST transitions."""

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from grelmicro.errors import SettingsValidationError
from grelmicro.task import (
    TaskRouter,
    Tasks,
    TasksConfig,
    TimezoneError,
)
from grelmicro.task._cron import CronExpression, CronTask, _delay_to_next_minute
from grelmicro.task._utils import resolve_timezone
from tests.task.samples import test1, test2, test3

if TYPE_CHECKING:
    from pytest_mock import MockFixture

pytestmark = [pytest.mark.timeout(10)]

SHUTDOWN_TIMEOUT = 5
SECONDS_PER_MINUTE = 60
EXPECTED_DELAY = 45
CLAMPED_WAKES = 2

ZURICH = ZoneInfo("Europe/Zurich")

# Europe/Zurich transitions used throughout. Fall back on 2026-10-25 turns
# 03:00 CEST into 02:00 CET, so every wall time in that hour happens twice.
# Spring forward on 2027-03-28 turns 02:00 CET into 03:00 CEST, so no wall
# time in that hour happens at all.
FALL_BACK_DAY = (2026, 10, 25)
SPRING_FORWARD_DAY = (2027, 3, 28)


def _cron_task(expr: str, timezone: str | None = None) -> CronTask:
    """Build a cron task without going through a decorator."""
    return CronTask(function=test1, expr=expr, timezone=timezone)


def test_timezone_defaults_to_none_until_resolved() -> None:
    """A task reports no timezone until a `Tasks` resolves one."""
    # Arrange / Act
    task = _cron_task("0 2 * * *")

    # Assert
    assert task.timezone is None


def test_declared_timezone_is_reported() -> None:
    """A task built with a timezone reports it before any resolution."""
    # Arrange / Act
    task = _cron_task("0 2 * * *", "Europe/Zurich")

    # Assert
    assert task.timezone == "Europe/Zurich"


def test_timezone_name_is_normalized() -> None:
    """A name given in any casing is stored the way the database spells it."""
    # Arrange / Act
    task = _cron_task("0 2 * * *", "europe/zurich")

    # Assert
    assert task.timezone == "Europe/Zurich"


def test_unknown_timezone_is_rejected_at_declaration() -> None:
    """An unusable name fails where it is written, not at the first fire."""
    # Act / Assert
    with pytest.raises(TimezoneError, match="unknown timezone name"):
        _cron_task("0 2 * * *", "Nope/Zone")


def test_abbreviation_that_names_no_zone_is_rejected() -> None:
    """`PST` is a DST abbreviation, not a zone, so it never reaches a fire."""
    # Act / Assert
    with pytest.raises(TimezoneError, match="unknown timezone name"):
        _cron_task("0 2 * * *", "PST")


async def test_tasks_timezone_reaches_every_cron_task() -> None:
    """The `Tasks` timezone applies to a task that declares none."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    task = _cron_task("0 2 * * *")
    tasks.add_task(task)

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        assert task.timezone == "Europe/Zurich"


async def test_declared_task_timezone_wins_over_tasks() -> None:
    """A task that declares a timezone keeps it."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    task = _cron_task("0 2 * * *", "UTC")
    tasks.add_task(task)

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        assert task.timezone == "UTC"


async def test_router_timezone_wins_over_tasks() -> None:
    """A router that declares a timezone overrides it for its own subtree."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    router = TaskRouter(timezone="America/Chicago")
    task = _cron_task("0 2 * * *")
    router.add_task(task)
    tasks.include_router(router)

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        assert task.timezone == "America/Chicago"


async def test_nested_router_inherits_through_the_tree() -> None:
    """A router that declares nothing passes the timezone further down."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    middle = TaskRouter()
    leaf = TaskRouter()
    task = _cron_task("0 2 * * *")
    leaf.add_task(task)
    middle.include_router(leaf)
    tasks.include_router(middle)

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        assert task.timezone == "Europe/Zurich"


async def test_resolution_does_not_depend_on_wiring_order() -> None:
    """A task added after its router was included still gets the timezone."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    router = TaskRouter()
    tasks.include_router(router)
    late = _cron_task("0 2 * * *")
    router.add_task(late)

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        assert late.timezone == "Europe/Zurich"


def test_router_reports_only_what_it_declares() -> None:
    """A router does not report a timezone it would merely inherit."""
    # Arrange
    declared = TaskRouter(timezone="Europe/Zurich")
    silent = TaskRouter()

    # Act / Assert
    assert declared.timezone == "Europe/Zurich"
    assert silent.timezone is None


def test_tasks_reports_its_resolved_timezone_immediately() -> None:
    """`Tasks` resolves its config at construction, so it always knows."""
    # Arrange / Act
    tasks = Tasks(timezone="Europe/Zurich")

    # Assert
    assert tasks.timezone == "Europe/Zurich"
    assert Tasks().timezone == "UTC"


async def test_resolution_refuses_to_move_a_running_task() -> None:
    """A task already running keeps the timezone it started with."""
    # Arrange
    task = _cron_task("0 2 * * *")
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich", tasks=[task])

    # Act
    async with tasks:
        await tasks.start()

        # Assert
        with pytest.raises(Exception, match="start"):
            tasks._resolve_timezones("America/Chicago")


def test_from_config_bypasses_the_environment() -> None:
    """A pre-built config is used as-is."""
    # Arrange / Act
    tasks = Tasks.from_config(
        TasksConfig(timezone="Europe/Zurich", shutdown_timeout=5)
    )

    # Assert
    assert tasks.timezone == "Europe/Zurich"
    assert tasks.config.shutdown_timeout == SHUTDOWN_TIMEOUT


def test_invalid_timezone_reports_a_settings_error() -> None:
    """`Tasks` reports a bad timezone through the settings error type."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="timezone"):
        Tasks(timezone="Nope/Zone")


def test_negative_shutdown_timeout_is_rejected() -> None:
    """A negative drain budget fails validation."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="shutdown_timeout"):
        Tasks(shutdown_timeout=-1)


class TestDaylightSaving:
    """Fire times across a daylight saving transition."""

    def test_fall_back_resolves_an_ambiguous_fire_once(self) -> None:
        """A wall time the clock passes twice is a single fire.

        Without this, the second pass computes a `due` above the durable
        last-fire state, wins the claim, and runs the body a second time.
        """
        # Arrange
        expr = CronExpression("30 2 * * *")
        first_pass = datetime(
            *FALL_BACK_DAY, 2, 30, tzinfo=ZURICH, fold=0
        ).timestamp()
        during_second_pass = datetime(
            *FALL_BACK_DAY, 2, 30, tzinfo=ZURICH, fold=1
        )

        # Act
        due = expr.previous_or_equal(during_second_pass)

        # Assert
        assert due is not None
        assert due.timestamp() == first_pass

    def test_fall_back_does_not_wait_on_a_past_instant(self) -> None:
        """Inside the repeated hour the next match resolves to the past.

        Waiting on that negative delay would return immediately and spin
        for the whole hour, one schedule backend read per iteration.
        """
        # Arrange
        expr = CronExpression("* * * * *")
        now = datetime(*FALL_BACK_DAY, 2, 30, tzinfo=ZURICH, fold=1)

        # Act
        delay = expr.next_after(now).timestamp() - now.timestamp()

        # Assert
        assert delay < 0
        assert 0 < _delay_to_next_minute(now) <= SECONDS_PER_MINUTE

    def test_delay_to_next_minute_never_returns_zero(self) -> None:
        """A boundary already reached still yields a positive wait."""
        # Arrange
        on_the_boundary = datetime(
            *FALL_BACK_DAY, 2, 30, 59, 999_999, tzinfo=ZURICH
        )

        # Act
        delay = _delay_to_next_minute(on_the_boundary)

        # Assert
        assert delay > 0

    def test_spring_forward_fires_once_past_the_gap(self) -> None:
        """A wall time that does not exist fires once, just after the jump."""
        # Arrange
        expr = CronExpression("30 2 * * *")
        before = datetime(2027, 3, 27, 12, 0, tzinfo=ZURICH)

        # Act
        fire = expr.next_after(before)
        real = datetime.fromtimestamp(fire.timestamp(), ZURICH)

        # Assert
        assert (fire.year, fire.month, fire.day) == SPRING_FORWARD_DAY
        # 02:30 never happens, so the fire lands at 03:30 real local time.
        assert (real.hour, real.minute) == (3, 30)
        # The day after the transition is back to a plain 02:30.
        following = expr.next_after(fire)
        assert (following.hour, following.minute) == (2, 30)


def test_interval_tasks_are_left_alone_by_resolution() -> None:
    """An interval task carries no timezone, so resolution skips it."""
    # Arrange
    tasks = Tasks(auto_start=False, timezone="Europe/Zurich")
    tasks.every(seconds=60)(test2)
    tasks.cron("0 2 * * *")(test3)

    # Act
    tasks._resolve_timezones(tasks.timezone)

    # Assert
    interval, cron = tasks.tasks
    assert not hasattr(interval, "timezone")
    assert isinstance(cron, CronTask)
    assert cron.timezone == "Europe/Zurich"


def test_resolving_an_unusable_name_reports_a_task_error() -> None:
    """Resolution failures surface as the task-module error type."""
    # Act / Assert
    with pytest.raises(TimezoneError, match="unknown timezone name"):
        resolve_timezone("Nope/Zone")


async def test_loop_waits_for_the_next_minute_inside_a_repeated_hour(
    mocker: "MockFixture",
) -> None:
    """The loop never waits on an instant that has already gone by.

    Inside the hour a fall-back transition repeats, the next matching
    minute resolves to the first pass, which is in the past. Waiting on
    that would return at once and spin for the whole hour.
    """
    # Arrange
    inside_repeated_hour = datetime(
        *FALL_BACK_DAY, 2, 30, 15, tzinfo=ZURICH, fold=1
    )

    def frozen_now(tz: object) -> datetime:  # noqa: ARG001
        return inside_repeated_hour

    mocker.patch("grelmicro.task._cron._now", side_effect=frozen_now)
    delays: list[float] = []

    async def record_delay(seconds: float, stop: object) -> bool:  # noqa: ARG001
        delays.append(seconds)
        return True

    mocker.patch("grelmicro.task._cron.sleep_or_stop", side_effect=record_delay)
    task = CronTask(function=test1, expr="* * * * *", timezone="Europe/Zurich")

    # Act
    await task()

    # Assert
    assert delays
    assert all(delay > 0 for delay in delays)
    assert delays[0] == pytest.approx(EXPECTED_DELAY, abs=1)


async def test_clamped_wake_does_not_run_the_body(
    mocker: "MockFixture",
) -> None:
    """Waking early inside the repeated hour is not a fire.

    Without a schedule backend every tick runs the body, so a wake that
    only exists because the next match lies in the past must not tick.
    """
    # Arrange: 02:06 on the second pass, so the next 02:30 match
    # resolves to the first pass and lies 36 minutes in the past.
    inside_repeated_hour = datetime(*FALL_BACK_DAY, 2, 6, tzinfo=ZURICH, fold=1)

    def frozen_now(tz: object) -> datetime:  # noqa: ARG001
        return inside_repeated_hour

    mocker.patch("grelmicro.task._cron._now", side_effect=frozen_now)
    runs = 0

    async def count_run() -> None:
        nonlocal runs
        runs += 1

    wakes = 0

    async def wake_twice(seconds: float, stop: object) -> bool:  # noqa: ARG001
        nonlocal wakes
        wakes += 1
        # Sleep through the clamp twice, then ask the loop to stop.
        return wakes > CLAMPED_WAKES

    mocker.patch("grelmicro.task._cron.sleep_or_stop", side_effect=wake_twice)
    task = CronTask(function=test1, expr="30 2 * * *", timezone="Europe/Zurich")
    mocker.patch.object(task, "_run", side_effect=count_run)

    # Act
    await task()

    # Assert
    assert wakes > CLAMPED_WAKES
    assert runs == 0
