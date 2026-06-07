"""Auto-instrumentation tests for the task component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.task._interval import IntervalTask

if TYPE_CHECKING:
    from tests.metrics.conftest import MetricsHarness

_ran = False


async def _work() -> None:
    """Module-level task body (tasks reject nested functions)."""
    global _ran  # noqa: PLW0603
    _ran = True


async def _boom() -> None:
    """Module-level failing task body."""
    raise ValueError


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
