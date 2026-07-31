"""Auto-instrumentation tests for the task component.

The tests below the success and error cases cover the fires that never
reach the body. Each one proves the failure is observable on
`grelmicro.task.runs`, so a refactor cannot silently reintroduce the
suppression that issue #605 removed.
"""

from __future__ import annotations

import asyncio
from asyncio import sleep
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

from grelmicro.coordination._protocol import LockPrimitive
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.memory import MemoryScheduleAdapter
from grelmicro.task._cron import CronTask
from grelmicro.task._interval import IntervalTask
from tests.task._helpers import cancel_group, start_task
from tests.task.samples import BadLock, WouldBlockLock

if TYPE_CHECKING:
    from types import TracebackType

    import pytest
    from pytest_mock import MockFixture

    from tests.metrics.conftest import MetricsHarness

EVERY_MINUTE = "* * * * *"
SLEEP = 0.01
WORKERS = 3
# A fixed instant 30 seconds past a whole minute, so the most recent fire
# is always 30 seconds old and a grace budget below that always expires.
PINNED = datetime(2026, 7, 31, 12, 0, 30, tzinfo=UTC)
PINNED_DUE = PINNED.replace(second=0).timestamp()

_ran = False


async def _work() -> None:
    """Module-level task body (tasks reject nested functions)."""
    global _ran  # noqa: PLW0603
    _ran = True


async def _boom() -> None:
    """Module-level failing task body."""
    raise ValueError


class _LockExpiredOnRelease(LockPrimitive):
    """Lock that enters cleanly, then fails on release like an expired lease."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Fail on release, after the body already ran and reported."""
        raise LockNotOwnedError(name="expired")


class _FailsOnRelease(LockPrimitive):
    """Lock that enters cleanly, then raises on release."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Fail on release, after the body already ran and reported."""
        msg = "release failed"
        raise RuntimeError(msg)


class _UnreachableSchedule(MemoryScheduleAdapter):
    """Schedule backend that cannot be reached."""

    async def last_fired(self, name: str) -> float | None:  # noqa: ARG002
        """Fail like an unreachable backend."""
        msg = "backend unreachable"
        raise ConnectionError(msg)


class _LosingSchedule(MemoryScheduleAdapter):
    """Schedule backend where every claim is won by a peer."""

    async def claim(self, name: str, due: float) -> bool:  # noqa: ARG002
        """Lose every claim."""
        return False


def _outcomes(harness: MetricsHarness) -> dict[str, dict[str, Any]]:
    """Return the `grelmicro.task.runs` attributes keyed by outcome."""
    return {
        attributes["outcome"]: attributes
        for _, attributes in harness.points("grelmicro.task.runs")
    }


def _pin_clock(mocker: MockFixture) -> None:
    """Pin the cron wall clock to `PINNED`."""
    mocker.patch("grelmicro.task._cron._now", PINNED.astimezone)


async def test_task_emits_success(metrics_reader: MetricsHarness) -> None:
    """A successful task run emits runs(outcome=success), duration, active."""
    task = IntervalTask(seconds=1, function=_work, name="cleanup")
    await task._run_with_sync([])

    runs = metrics_reader.points("grelmicro.task.runs")
    assert runs[0][1] == {"task.name": "cleanup", "outcome": "success"}
    assert metrics_reader.points("grelmicro.task.duration")[0][1] == {
        "task.name": "cleanup"
    }
    assert metrics_reader.points("grelmicro.task.active")[0][0] == 0


async def test_task_emits_error(metrics_reader: MetricsHarness) -> None:
    """A failing task run emits runs(outcome=error) with the error type."""
    task = IntervalTask(seconds=1, function=_boom, name="boom")
    await task._run_with_sync([])

    runs = metrics_reader.points("grelmicro.task.runs")
    assert runs[0][1] == {
        "task.name": "boom",
        "outcome": "error",
        "error.type": "ValueError",
    }
    assert metrics_reader.points("grelmicro.task.active")[0][0] == 0


async def test_task_metrics_noop_when_off() -> None:
    """A task runs without error when no Metrics component is active."""
    global _ran  # noqa: PLW0603
    _ran = False
    task = IntervalTask(seconds=1, function=_work, name="svc")
    await task._run_with_sync([])
    assert _ran


# --- Fires that never reach the body ---


async def test_interval_task_emits_coordination_error(
    metrics_reader: MetricsHarness,
) -> None:
    """A lock that fails to acquire emits runs(outcome=coordination_error)."""
    task = IntervalTask(seconds=1, function=_work, name="sync", sync=BadLock())

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    assert _outcomes(metrics_reader)["coordination_error"] == {
        "task.name": "sync",
        "outcome": "coordination_error",
        "error.type": "ValueError",
    }
    assert task.last_fire is not None
    assert task.last_fire.outcome == "coordination_error"


async def test_interval_task_emits_skipped(
    metrics_reader: MetricsHarness,
) -> None:
    """A fire a peer already holds emits runs(outcome=skipped)."""
    task = IntervalTask(
        seconds=1, function=_work, name="peer", sync=WouldBlockLock()
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    assert _outcomes(metrics_reader)["skipped"] == {
        "task.name": "peer",
        "outcome": "skipped",
    }


async def test_interval_task_counts_expired_lock_fire_once(
    metrics_reader: MetricsHarness,
) -> None:
    """A lock expiring on release does not count the fire a second time.

    The body already ran and already reported, so a second point would
    inflate the total the counter exists to give.
    """
    task = IntervalTask(
        seconds=1,
        function=_work,
        name="expired",
        sync=_LockExpiredOnRelease(),
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    assert set(_outcomes(metrics_reader)) == {"success"}


async def test_interval_task_counts_fire_once_when_release_fails(
    metrics_reader: MetricsHarness,
) -> None:
    """A sync lock failing on release does not count the fire twice."""
    task = IntervalTask(
        seconds=1, function=_work, name="release", sync=_FailsOnRelease()
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    assert set(_outcomes(metrics_reader)) == {"success"}


async def test_cron_task_emits_coordination_error(
    metrics_reader: MetricsHarness,
) -> None:
    """An unreachable schedule backend emits runs(outcome=coordination_error)."""
    backend = _UnreachableSchedule()
    await backend.__aenter__()
    task = CronTask(
        expr=EVERY_MINUTE, function=_work, name="down", backend=backend
    )

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["coordination_error"] == {
        "task.name": "down",
        "outcome": "coordination_error",
        "error.type": "ConnectionError",
    }
    assert task.last_fire is not None
    assert task.last_fire.outcome == "coordination_error"


async def test_cron_task_emits_skipped_when_peer_claimed_first(
    metrics_reader: MetricsHarness, mocker: MockFixture
) -> None:
    """A fire a peer claimed while this worker was reading emits skipped.

    This is where most losing workers land: the peer's claim commits
    before this worker reads `last_fired`, so the fire reads as handled.
    """
    _pin_clock(mocker)
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    task = CronTask(
        expr=EVERY_MINUTE, function=_work, name="late-read", backend=backend
    )
    await backend.claim("late-read", PINNED_DUE)

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["skipped"] == {
        "task.name": "late-read",
        "outcome": "skipped",
    }
    assert task.last_fire is not None
    assert task.last_fire.outcome == "skipped"


async def test_cron_task_emits_skipped_when_peer_holds_the_lock(
    metrics_reader: MetricsHarness, mocker: MockFixture
) -> None:
    """A fire a peer holds emits runs(outcome=skipped).

    With no schedule backend every worker fires, so a lock refusing to
    admit the body means a peer is running it. Nothing is lost.
    """
    _pin_clock(mocker)
    task = CronTask(
        expr=EVERY_MINUTE,
        function=_work,
        name="held",
        sync=WouldBlockLock(),
    )

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["skipped"] == {
        "task.name": "held",
        "outcome": "skipped",
    }
    assert task.last_fire is not None
    assert task.last_fire.outcome == "skipped"


async def test_cron_task_emits_missed_when_claimed_but_not_admitted(
    metrics_reader: MetricsHarness,
    mocker: MockFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A claimed fire the body cannot be admitted to is missed, not skipped.

    The claim advanced the baseline, so no peer replays the fire. Calling
    that a skip would label a lost fire as healthy sharing.
    """
    _pin_clock(mocker)
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    backend._last_fired["unadmitted"] = PINNED_DUE - 120
    task = CronTask(
        expr=EVERY_MINUTE,
        function=_work,
        name="unadmitted",
        backend=backend,
        sync=WouldBlockLock(),
    )

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["missed"] == {
        "task.name": "unadmitted",
        "outcome": "missed",
    }
    assert any(
        "claimed but not admitted" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


async def test_cron_task_emits_skipped_when_claim_lost(
    metrics_reader: MetricsHarness,
) -> None:
    """A claim a peer wins emits runs(outcome=skipped)."""
    backend = _LosingSchedule()
    await backend.__aenter__()
    backend._last_fired["lost"] = datetime.now(UTC).timestamp() - 120
    task = CronTask(
        expr=EVERY_MINUTE, function=_work, name="lost", backend=backend
    )

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["skipped"] == {
        "task.name": "lost",
        "outcome": "skipped",
    }


async def test_cron_task_catchup_tick_reports_nothing(
    metrics_reader: MetricsHarness, mocker: MockFixture
) -> None:
    """A startup catch-up with nothing missed is not a skipped fire."""
    _pin_clock(mocker)
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    task = CronTask(
        expr=EVERY_MINUTE, function=_work, name="catchup", backend=backend
    )
    await backend.claim("catchup", PINNED_DUE)

    await task._tick_guarded(catchup=True)

    assert _outcomes(metrics_reader) == {}
    assert task.last_fire is None


async def test_cron_task_emits_missed_when_too_late_to_replay(
    metrics_reader: MetricsHarness,
    mocker: MockFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fire dropped past the grace budget emits runs(outcome=missed).

    No worker runs it, so it is not a skip. Before this it had no log
    and no metric at all.
    """
    _pin_clock(mocker)
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    backend._last_fired["dropped"] = PINNED_DUE - 120
    task = CronTask(
        expr=EVERY_MINUTE,
        function=_work,
        name="dropped",
        backend=backend,
        misfire_grace_seconds=1,
    )

    await task._tick_guarded(catchup=False)

    assert _outcomes(metrics_reader)["missed"] == {
        "task.name": "dropped",
        "outcome": "missed",
    }
    assert task.last_fire is not None
    assert task.last_fire.outcome == "missed"
    assert any(
        "Task fire missed" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


async def test_cron_task_emits_skipped_when_peer_took_the_dropped_fire(
    metrics_reader: MetricsHarness, mocker: MockFixture
) -> None:
    """A peer that records a dropped fire leaves this worker skipped.

    The peer handled the fire even though nobody ran it, so this worker
    stands down rather than reporting the same drop twice.
    """
    _pin_clock(mocker)
    backend = _LosingSchedule()
    await backend.__aenter__()
    backend._last_fired["taken"] = PINNED_DUE - 120
    task = CronTask(
        expr=EVERY_MINUTE,
        function=_work,
        name="taken",
        backend=backend,
        misfire_grace_seconds=1,
    )

    await task._tick_guarded(catchup=False)

    assert set(_outcomes(metrics_reader)) == {"skipped"}


async def test_cron_task_counts_fire_once_when_release_fails(
    metrics_reader: MetricsHarness,
) -> None:
    """A sync lock failing on release does not count the fire twice."""
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    backend._last_fired["release"] = datetime.now(UTC).timestamp() - 120
    task = CronTask(
        expr=EVERY_MINUTE,
        function=_work,
        name="release",
        backend=backend,
        sync=_FailsOnRelease(),
    )

    await task._tick_guarded(catchup=False)

    assert set(_outcomes(metrics_reader)) == {"success"}


async def test_cron_task_missed_fire_counted_once_across_workers(
    metrics_reader: MetricsHarness, mocker: MockFixture
) -> None:
    """Only the worker that advances the baseline reports a dropped fire."""
    _pin_clock(mocker)
    backend = MemoryScheduleAdapter()
    await backend.__aenter__()
    backend._last_fired["shared"] = PINNED_DUE - 120
    workers = [
        CronTask(
            expr=EVERY_MINUTE,
            function=_work,
            name="shared",
            backend=backend,
            misfire_grace_seconds=1,
        )
        for _ in range(WORKERS)
    ]

    for worker in workers:
        await worker._tick_guarded(catchup=False)

    points = {
        attributes["outcome"]: value
        for value, attributes in metrics_reader.points("grelmicro.task.runs")
    }
    assert points["missed"] == 1
    assert points["skipped"] == WORKERS - 1
