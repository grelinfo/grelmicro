"""One fire of a scheduled task, recorded the same way for every kind of task."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.errors import WouldBlockError
from grelmicro.task._fire import FireOutcome, FireRecorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.metrics.conftest import MetricsHarness

STARTED = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
DELAY = 1.5


async def _work() -> None:
    """Return at once."""


async def _boom() -> None:
    """Fail the body."""
    raise ValueError


async def _hang() -> None:
    """Run until cancelled."""
    await asyncio.Event().wait()


def _recorder(function: Callable[..., Any] = _work) -> FireRecorder:
    """Build a recorder whose clock always reads `STARTED`."""
    return FireRecorder("cleanup", function, clock=lambda: STARTED)


class TestRun:
    """Running the body records the fire, whatever it did."""

    async def test_a_body_that_returns_is_a_success(
        self, metrics_reader: MetricsHarness
    ) -> None:
        """The fire is recorded once, with its start and the delay before it."""
        fire = _recorder()

        await fire.run(STARTED, DELAY)

        assert fire.started
        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.SUCCESS
        assert fire.last.started_at == STARTED
        assert metrics_reader.points("grelmicro.task.runs")[0][1] == {
            "grelmicro.task.name": "cleanup",
            "grelmicro.outcome": "success",
        }
        [(delay, _)] = metrics_reader.points("grelmicro.task.schedule.delay")
        assert delay == DELAY
        assert metrics_reader.points("grelmicro.task.active")[0][0] == 0

    async def test_a_body_that_raises_is_an_error(
        self, metrics_reader: MetricsHarness
    ) -> None:
        """The error is recorded with its type and does not propagate."""
        fire = _recorder(_boom)

        await fire.run(STARTED, None)

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.ERROR
        assert metrics_reader.points("grelmicro.task.runs")[0][1] == {
            "grelmicro.task.name": "cleanup",
            "grelmicro.outcome": "error",
            "error.type": "ValueError",
        }
        assert not metrics_reader.points("grelmicro.task.schedule.delay")

    async def test_a_cancelled_body_is_recorded_as_an_error(
        self, metrics_reader: MetricsHarness
    ) -> None:
        """Cancellation propagates, and the fire still counts its duration."""
        fire = _recorder(_hang)
        runner = asyncio.create_task(fire.run(STARTED, None))
        await asyncio.sleep(0)

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.ERROR
        assert metrics_reader.points("grelmicro.task.duration")
        assert metrics_reader.points("grelmicro.task.active")[0][0] == 0


class TestGuard:
    """A fire kept from the body is recorded by what kept it."""

    async def test_a_refused_admission_is_skipped(self) -> None:
        """A fire whose lock would block never ran, and is a skip."""
        fire = _recorder()

        async def refused() -> None:
            raise WouldBlockError

        await fire.guard(refused())

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.SKIPPED
        assert fire.last.started_at == STARTED

    async def test_a_failure_before_the_body_is_a_coordination_error(
        self, metrics_reader: MetricsHarness
    ) -> None:
        """Coordination failing before the body is reported once, typed."""
        fire = _recorder()

        async def unreachable() -> None:
            raise ConnectionError

        await fire.guard(unreachable())

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.COORDINATION_ERROR
        assert metrics_reader.points("grelmicro.task.runs")[0][1] == {
            "grelmicro.task.name": "cleanup",
            "grelmicro.outcome": "coordination_error",
            "error.type": "ConnectionError",
        }

    async def test_a_failure_after_the_body_keeps_the_body_outcome(
        self, metrics_reader: MetricsHarness
    ) -> None:
        """A release failing after the body ran never counts the fire twice."""
        fire = _recorder()

        async def ran_then_failed() -> None:
            await fire.run(STARTED, None)
            raise ConnectionError

        await fire.guard(ran_then_failed())

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.SUCCESS
        assert len(metrics_reader.points("grelmicro.task.runs")) == 1

    async def test_a_lock_lost_on_release_keeps_the_body_outcome(self) -> None:
        """The body already reported its fire, so nothing is added."""
        fire = _recorder()

        async def lost() -> None:
            await fire.run(STARTED, None)
            raise LockNotOwnedError(name="cleanup")

        await fire.guard(lost())

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.SUCCESS

    async def test_each_guard_starts_a_new_fire(self) -> None:
        """A body that started on one tick does not mark the next one."""
        fire = _recorder()
        await fire.guard(fire.run(STARTED, None))

        async def unreachable() -> None:
            raise ConnectionError

        await fire.guard(unreachable())

        assert fire.last is not None
        assert fire.last.outcome is FireOutcome.COORDINATION_ERROR

    async def test_cancellation_propagates(self) -> None:
        """A cancelled tick is never swallowed as an error."""
        fire = _recorder(_hang)
        runner = asyncio.create_task(fire.guard(fire.run(STARTED, None)))
        await asyncio.sleep(0)

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
