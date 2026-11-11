"""Test the metrics an on-demand job records."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.task import JobStatus, Progress, Tasks
from grelmicro.task._protocol import Beat, BeatReply, Settled
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

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
        info = await job.without_owner().get(job_id)
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
        submitted = await job.without_owner().submit(fail)
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
        submitted = await job.without_owner().submit()
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        await job.without_owner().cancel(submitted.id)
        await _until_finished(job, submitted.id)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None
    assert info.status is JobStatus.CANCELLED
    assert not metrics_reader.points("grelmicro.task.runs")
    [(_, attributes)] = metrics_reader.points("grelmicro.task.duration")
    assert attributes == {
        "grelmicro.task.name": "import",
        "grelmicro.outcome": "error",
    }


async def _takes_an_int(_progress: Progress, count: int) -> None:
    """Take an int."""
    _ = count


async def test_job_arguments_that_cannot_load_count_as_an_error(
    metrics_reader: MetricsHarness,
) -> None:
    """Arguments that no longer load land on `grelmicro.task.runs`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="import")(_takes_an_int)

    async with tasks:
        # Act
        await job._backend.submit(
            name=job.name,
            job_id="stale",
            owner=None,
            payload='{"count": "not an int"}',
            context={},
            max_queued=None,
            max_wait=None,
            max_run=None,
            keep=None,
            start_as=None,
            lease=timedelta(seconds=30),
        )
        job._wake_loop()
        await _until_finished(job, "stale")

    # Assert
    assert metrics_reader.points("grelmicro.task.runs") == [
        (
            1,
            {
                "grelmicro.task.name": "import",
                "grelmicro.outcome": "error",
                "error.type": "ValidationError",
            },
        )
    ]


class _LosesEveryJob(MemoryJobAdapter):
    """A store that no longer gives the runner any job it ends."""

    async def settle(self, **_kwargs: object) -> Settled:
        return Settled(settled=False, error=None)


async def test_job_lost_at_settle_counts_an_error_not_a_success(
    metrics_reader: MetricsHarness,
) -> None:
    """A body that returned on a job the store lost counts as lost."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="import", backend=_LosesEveryJob())(_work)

    async with tasks:
        # Act
        await job.without_owner().submit(False)  # noqa: FBT003
        for _ in range(10):
            await asyncio.sleep(0)

    # Assert
    assert metrics_reader.points("grelmicro.task.runs") == [
        (
            1,
            {
                "grelmicro.task.name": "import",
                "grelmicro.outcome": "error",
                "error.type": "job_lost",
            },
        )
    ]


async def _swallow_cancel(_progress: Progress) -> str:
    """Catch the cancellation and return JSON."""
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
    return "done"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_hang, id="stopped"),
        pytest.param(_swallow_cancel, id="returned"),
    ],
)
async def test_job_past_max_run_counts_a_timed_out_error(
    metrics_reader: MetricsHarness, body: Callable[..., Any]
) -> None:
    """A run past `max_run` counts as an error of type `job_timed_out`."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(name="import", max_run=5)(body)

    async with clock, tasks:
        # Act
        submitted = await job.without_owner().submit()
        for _ in range(7):
            await clock.advance(1)
            for _ in range(5):
                await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)

    # Assert
    assert info is not None
    assert info.error == "Job not finished in time"
    assert metrics_reader.points("grelmicro.task.runs") == [
        (
            1,
            {
                "grelmicro.task.name": "import",
                "grelmicro.outcome": "error",
                "error.type": "job_timed_out",
            },
        )
    ]
    [(_, attributes)] = metrics_reader.points("grelmicro.task.duration")
    assert attributes["error.type"] == "job_timed_out"


class _ReportsNothing(MemoryJobAdapter):
    """A store whose beats find every job still held."""

    async def beat(
        self, *, beats: Sequence[Beat], **_kwargs: object
    ) -> list[BeatReply]:
        return [
            BeatReply(beat.name, beat.job_id, held=True, cancel_requested=False)
            for beat in beats
        ]


async def _wait_for(_progress: Progress, gate: str) -> str:
    """Return once the shared `gate` is set."""
    await shared(gate).wait()
    return "done"


async def test_job_returned_past_max_run_before_a_beat_counts_timed_out(
    metrics_reader: MetricsHarness,
) -> None:
    """A body that returns past `max_run`, before any beat said so, times out."""
    # Arrange
    clock = VirtualClock()
    gate = asyncio.Event()
    tasks = Tasks()
    job = tasks.job(name="import", max_run=5, backend=_ReportsNothing())(
        _wait_for
    )

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        for _ in range(3):
            await asyncio.sleep(0)
        # Act
        await clock.advance(7)
        gate.set()
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None
    assert info.error == "Job not finished in time"
    assert metrics_reader.points("grelmicro.task.runs") == [
        (
            1,
            {
                "grelmicro.task.name": "import",
                "grelmicro.outcome": "error",
                "error.type": "job_timed_out",
            },
        )
    ]


_TIMED_OUT = [
    (
        1,
        {
            "grelmicro.task.name": "import",
            "grelmicro.outcome": "error",
            "error.type": "job_timed_out",
        },
    )
]
"""The one run a job past `max_run` counts."""


async def _raises_after(_progress: Progress, gate: str) -> str:
    """Raise once the shared `gate` is set."""
    await shared(gate).wait()
    raise ValueError


async def _slow_to_stop(_progress: Progress, gate: str) -> str:
    """Stop on a cancel only once the shared `gate` is set."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await shared(gate).wait()
        raise
    return "unreachable"


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(_ReportsNothing, id="before-a-beat"),
        pytest.param(MemoryJobAdapter, id="after-a-beat"),
    ],
)
async def test_job_raised_past_max_run_counts_timed_out(
    metrics_reader: MetricsHarness, backend: type[MemoryJobAdapter]
) -> None:
    """A body that raises past `max_run` counts the timeout, not its error."""
    # Arrange
    clock = VirtualClock()
    gate = asyncio.Event()
    tasks = Tasks()
    job = tasks.job(name="import", max_run=5, backend=backend())(_raises_after)

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        await clock.advance(7)
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None
    assert info.error == "Job not finished in time"
    assert metrics_reader.points("grelmicro.task.runs") == _TIMED_OUT


async def test_job_cancel_that_stops_past_max_run_counts_timed_out(
    metrics_reader: MetricsHarness,
) -> None:
    """A cancel the body ends only past `max_run` counts the timeout."""
    # Arrange
    clock = VirtualClock()
    gate = asyncio.Event()
    tasks = Tasks()
    job = tasks.job(name="import", max_run=5, backend=_ReportsNothing())(
        _slow_to_stop
    )

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        for _ in range(5):
            await asyncio.sleep(0)
        await clock.advance(2)
        await job.without_owner().cancel(submitted.id)
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        await clock.advance(7)
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None
    assert info.error == "Job not finished in time"
    assert metrics_reader.points("grelmicro.task.runs") == _TIMED_OUT


def _sync_raises(_progress: Progress, release: str) -> str:
    """Raise once the shared `release` is set, with no checkpoint."""
    shared(release).wait()
    raise ValueError


async def test_job_sync_body_raising_past_max_run_counts_timed_out(
    metrics_reader: MetricsHarness,
) -> None:
    """A sync body that raises past `max_run` counts the timeout."""
    # Arrange
    clock = VirtualClock()
    release = threading.Event()
    tasks = Tasks()
    job = tasks.job(name="import", max_run=5)(_sync_raises)

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(release))
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        for _ in range(7):
            await clock.advance(1)
            for _ in range(5):
                await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)
        release.set()
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0.001)

    # Assert
    assert info is not None
    assert info.error == "Job not finished in time"
    assert metrics_reader.points("grelmicro.task.runs") == _TIMED_OUT


async def test_job_raised_past_its_lease_counts_lost(
    metrics_reader: MetricsHarness,
) -> None:
    """A body that raises after its lease lapsed counts the job lost."""
    # Arrange
    clock = VirtualClock()
    gate = asyncio.Event()
    tasks = Tasks()
    job = tasks.job(name="import", backend=_ReportsNothing())(_raises_after)

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        await clock.advance(31)
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None
    assert info.error == "Job lost"
    assert metrics_reader.points("grelmicro.task.runs") == [
        (
            1,
            {
                "grelmicro.task.name": "import",
                "grelmicro.outcome": "error",
                "error.type": "job_lost",
            },
        )
    ]
