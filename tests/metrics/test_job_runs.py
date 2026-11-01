"""Test the metrics an on-demand job records."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from grelmicro.task import JobStatus, Progress, Tasks

if TYPE_CHECKING:
    from grelmicro.task import Job
    from tests.metrics.conftest import MetricsHarness

pytestmark = [pytest.mark.timeout(10)]


async def _work(_progress: Progress, fail: bool) -> None:  # noqa: FBT001
    """Return, or raise when `fail`."""
    if fail:
        raise ValueError


async def _hang(_progress: Progress) -> None:
    """Run until cancelled."""
    await asyncio.Event().wait()


async def _until_finished(job: Job[...], job_id: str) -> None:
    """Wait until the job `job_id` reaches an end state."""
    while True:
        info = await job.get(job_id)
        if info is not None and info.finished_at is not None:
            return
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("fail", "outcome"),
    [
        pytest.param(False, {"grelmicro.outcome": "success"}, id="success"),
        pytest.param(
            True,
            {"grelmicro.outcome": "error", "error.type": "ValueError"},
            id="error",
        ),
    ],
)
async def test_job_run_counts_under_the_job_name(
    metrics_reader: MetricsHarness,
    fail: bool,  # noqa: FBT001
    outcome: dict[str, str],
) -> None:
    """Each run lands on `grelmicro.task.runs` like a scheduled fire."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="import")(_work)

    async with tasks:
        # Act
        submitted = await job.submit(fail)
        await _until_finished(job, submitted.id)

    # Assert
    assert metrics_reader.points("grelmicro.task.runs") == [
        (1, {"grelmicro.task.name": "import", **outcome})
    ]
    assert metrics_reader.points("grelmicro.task.active")[0][0] == 0


async def test_job_cancelled_records_its_duration_as_an_error(
    metrics_reader: MetricsHarness,
) -> None:
    """A cancelled run counts no run, and its duration reads as an error."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="import")(_hang)

    async with tasks:
        submitted = await job.submit()
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        await job.cancel(submitted.id)
        await _until_finished(job, submitted.id)
        info = await job.get(submitted.id)

    # Assert
    assert info is not None
    assert info.status is JobStatus.CANCELLED
    assert not metrics_reader.points("grelmicro.task.runs")
    [(_, attributes)] = metrics_reader.points("grelmicro.task.duration")
    assert attributes == {
        "grelmicro.task.name": "import",
        "grelmicro.outcome": "error",
    }
