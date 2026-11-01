"""Test on-demand jobs."""

import asyncio
import contextlib
import contextvars
import gc
import logging
import threading
import weakref
from datetime import timedelta
from typing import Annotated

import pytest
from fast_depends import Depends
from pydantic import JsonValue

from grelmicro.clock import VirtualClock
from grelmicro.errors import AdmissionError, OutOfContextError
from grelmicro.task import (
    Job,
    JobError,
    JobInfo,
    JobRefusedError,
    JobStatus,
    OwnedJob,
    Progress,
    Tasks,
)

pytestmark = [pytest.mark.timeout(10)]


async def _settle(job: Job[...], job_id: str) -> JobInfo:
    """Poll `job_id` until it reaches an end state, and return it."""
    while True:
        info = await job.get(job_id)
        assert info is not None
        if info.finished_at is not None:
            return info
        await asyncio.sleep(0)


async def _count(progress: Progress, items: list[str]) -> None:
    """Report one result per item."""
    progress.total = len(items)
    for item in items:
        progress.advance(result=item.upper())


async def test_job_submit_runs_and_succeeds() -> None:
    """A submitted job runs in the background and ends `succeeded`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count)

    async with tasks:
        # Act
        submitted = await job.submit(["a", "b"])
        info = await _settle(job, submitted.id)

    # Assert
    assert submitted.status in {JobStatus.QUEUED, JobStatus.RUNNING}
    assert info.id == submitted.id
    assert info.name == "tests.task.test_job:_count"
    assert info.status is JobStatus.SUCCEEDED
    assert (info.done, info.total) == (2, 2)
    assert info.results == ("A", "B")
    assert info.error is None
    assert info.cancel_requested is False
    assert info.started_at is not None
    assert info.finished_at is not None
    assert info.created_at <= info.started_at <= info.finished_at


async def _fail_after_one(progress: Progress, message: str | None) -> None:
    """Report one result, then raise a `JobError` or a plain error."""
    progress.advance(result="first")
    if message is None:
        msg = "secret dsn=postgres://user:pass@db"
        raise RuntimeError(msg)
    raise JobError(message)


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        pytest.param(None, "Job failed", id="plain-error-is-hidden"),
        pytest.param("Row 3 is invalid", "Row 3 is invalid", id="job-error"),
        pytest.param("", "Job failed", id="empty-job-error"),
    ],
)
async def test_job_failure_keeps_partial_results(
    message: str | None, shown: str
) -> None:
    """A body that raises ends `failed`, with only safe text in `error`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_fail_after_one)

    async with tasks:
        # Act
        submitted = await job.submit(message)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.error == shown
    assert info.results == ("first",)
    assert info.done == 1


@pytest.mark.parametrize(
    ("message", "level", "traceback"),
    [
        pytest.param(None, logging.ERROR, True, id="plain-error"),
        pytest.param(
            "Row 3 is invalid", logging.WARNING, False, id="job-error"
        ),
    ],
)
async def test_job_failure_logs_a_traceback_only_for_an_unexpected_error(
    caplog: pytest.LogCaptureFixture,
    message: str | None,
    level: int,
    traceback: bool,  # noqa: FBT001
) -> None:
    """A `JobError` logs one warning line, any other error a traceback."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_fail_after_one)
    caplog.set_level(logging.WARNING, logger="grelmicro.task")

    async with tasks:
        # Act
        submitted = await job.submit(message)
        await _settle(job, submitted.id)

    # Assert
    [record] = [r for r in caplog.records if r.name == "grelmicro.task"]
    assert record.levelno == level
    assert (record.exc_info is not None) is traceback


async def _reject_row() -> None:
    """Reject a row with a message for the caller."""
    msg = "Row 3 is invalid"
    raise JobError(msg)


async def _fail_plainly() -> None:
    """Fail with an error that is not for the caller."""
    raise RuntimeError


async def _fan_out(_progress: Progress, mixed: bool) -> None:  # noqa: FBT001
    """Handle rows in a task group where one child rejects its row."""
    async with asyncio.TaskGroup() as group:
        group.create_task(_reject_row())
        if mixed:
            group.create_task(_fail_plainly())


@pytest.mark.parametrize(
    ("mixed", "shown", "level"),
    [
        pytest.param(
            False, "Row 3 is invalid", logging.WARNING, id="job-errors"
        ),
        pytest.param(True, "Job failed", logging.ERROR, id="mixed-errors"),
    ],
)
async def test_job_error_inside_a_task_group_reaches_the_caller(
    caplog: pytest.LogCaptureFixture,
    mixed: bool,  # noqa: FBT001
    shown: str,
    level: int,
) -> None:
    """A group of `JobError`s keeps its message, a mixed group does not."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_fan_out)
    caplog.set_level(logging.WARNING, logger="grelmicro.task")

    async with tasks:
        # Act
        submitted = await job.submit(mixed)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.error == shown
    [record] = [r for r in caplog.records if r.name == "grelmicro.task"]
    assert record.levelno == level
    assert (record.exc_info is None) is (not mixed)


def _count_in_thread(progress: Progress, items: list[str]) -> None:
    """Report one result per item from a worker thread."""
    progress.total = len(items)
    for _ in items:
        progress.advance(result=threading.current_thread().name)


async def test_job_sync_body_runs_in_a_worker_thread() -> None:
    """A sync body runs off the event loop and reports like an async one."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count_in_thread)

    async with tasks:
        # Act
        submitted = await job.submit(["a"])
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert info.done == 1
    assert info.results != (threading.current_thread().name,)


async def _noop(progress: Progress) -> None:
    """Do nothing."""


_OWNERS = [None, "alice", "bob"]


def _as[**P](job: Job[P], owner: str | None) -> Job[P] | OwnedJob[P]:
    """Return `job` as `owner` sees it, or as is for no owner."""
    return job if owner is None else job.for_owner(owner)


@pytest.mark.parametrize("submitted_by", _OWNERS)
@pytest.mark.parametrize("read_by", _OWNERS)
async def test_job_get_finds_a_job_only_for_its_owner(
    submitted_by: str | None, read_by: str | None
) -> None:
    """`get` returns a job only to the exact owner it was submitted with."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        submitted = await _as(job, submitted_by).submit()
        # Act
        info = await _as(job, read_by).get(submitted.id)

    # Assert
    if read_by == submitted_by:
        assert info is not None
        assert info.id == submitted.id
    else:
        assert info is None


async def test_job_get_returns_none_for_an_unknown_id() -> None:
    """`get` returns `None` for an id no job has."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act
        info = await job.get("unknown")

    # Assert
    assert info is None


async def _wait_for(_progress: Progress, gate: asyncio.Event) -> None:
    """Run until `gate` is set."""
    await gate.wait()


async def _statuses(job: Job[...], ids: list[str]) -> list[JobStatus]:
    """Return the status of each job in `ids`, after the loop settles."""
    for _ in range(5):
        await asyncio.sleep(0)
    infos = [await job.get(job_id) for job_id in ids]
    return [info.status for info in infos if info is not None]


_USER: contextvars.ContextVar[str] = contextvars.ContextVar("user")
"""Who submitted the job, as a request context would carry it."""


async def _record_user(
    _progress: Progress, seen: list[str], gate: asyncio.Event
) -> None:
    """Record the submitter the body sees, then wait for `gate`."""
    seen.append(_USER.get("nobody"))
    await gate.wait()


async def _submit_as(
    job: Job[[list[str], asyncio.Event]],
    user: str,
    seen: list[str],
    gate: asyncio.Event,
) -> str:
    """Submit from a context where `user` is the submitter."""
    _USER.set(user)
    return (await job.submit(seen, gate)).id


async def test_job_runs_in_the_context_of_its_own_submitter() -> None:
    """A queued job sees its own submitter's context, not an earlier one."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=1)(_record_user)
    seen: list[str] = []
    gate = asyncio.Event()

    async with tasks:
        # Act
        await asyncio.create_task(_submit_as(job, "alice", seen, gate))
        bob = await asyncio.create_task(_submit_as(job, "bob", seen, gate))
        gate.set()
        await _settle(job, bob)

    # Assert
    assert seen == ["alice", "bob"]


async def test_job_max_concurrent_queues_the_rest_in_order() -> None:
    """Past `max_concurrent`, jobs wait and then start in submission order."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=2)(_wait_for)
    gates = [asyncio.Event() for _ in range(4)]

    async with tasks:
        ids = [(await job.submit(gate)).id for gate in gates]
        # Act
        before = await _statuses(job, ids)
        gates[0].set()
        after_one = await _statuses(job, ids)
        for gate in gates:
            gate.set()
        settled = [await _settle(job, job_id) for job_id in ids]

    # Assert
    assert before == ["running", "running", "queued", "queued"]
    assert after_one == ["succeeded", "running", "running", "queued"]
    assert [info.status for info in settled] == ["succeeded"] * 4


_SUBMITS = 6


@pytest.mark.parametrize(
    ("max_queued", "accepted"),
    [
        pytest.param(0, 1, id="zero-refuses-once-every-slot-runs"),
        pytest.param(2, 3, id="bound"),
        pytest.param(None, _SUBMITS, id="no-limit"),
    ],
)
async def test_job_submit_refused_when_the_queue_is_full(
    max_queued: int | None, accepted: int
) -> None:
    """Past `max_queued` waiting jobs, `submit` raises `JobRefusedError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=1, max_queued=max_queued)(_wait_for)
    gate = asyncio.Event()

    async with tasks:
        # Act
        submitted = 0
        try:
            for _ in range(_SUBMITS):
                await job.submit(gate)
                submitted += 1
        except JobRefusedError as exc:
            refusal: JobRefusedError | None = exc
        else:
            refusal = None
        gate.set()

    # Assert
    assert submitted == accepted
    if accepted < _SUBMITS:
        assert isinstance(refusal, AdmissionError)
    else:
        assert refusal is None


@pytest.mark.parametrize(
    ("keep", "kept_for"),
    [
        pytest.param(0, None, id="zero-keeps-nothing"),
        pytest.param(60, 60, id="seconds"),
        pytest.param(timedelta(minutes=2), 120, id="timedelta"),
    ],
)
async def test_job_keep_forgets_a_finished_job_after_it(
    keep: int | timedelta, kept_for: int | None
) -> None:
    """A finished job stays readable for `keep`, then reads as unknown."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(keep=keep)(_noop)

    async with clock, tasks:
        submitted = await job.submit()
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        at_finish = await job.get(submitted.id)
        if kept_for is not None:
            await clock.advance(kept_for - 1)
        just_before = await job.get(submitted.id)
        await clock.advance(1)
        after = await job.get(submitted.id)

    # Assert
    if kept_for is None:
        assert at_finish is None
    else:
        assert at_finish is not None
        assert at_finish.status is JobStatus.SUCCEEDED
        assert just_before is not None
    assert after is None


async def test_job_keep_frees_an_expired_job_with_no_call_after_it() -> None:
    """A job past `keep` leaves memory even when nothing reads the job."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(keep=60)(_noop)

    async with clock, tasks:
        await job.submit()
        for _ in range(5):
            await asyncio.sleep(0)
        kept = len(job._records)
        # Act
        await clock.advance(60)
        for _ in range(5):
            await asyncio.sleep(0)
        left = len(job._records)

    # Assert
    assert (kept, left) == (1, 0)


async def test_job_keep_none_keeps_a_finished_job() -> None:
    """`keep=None` keeps a finished job readable with no time limit."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(keep=None)(_noop)

    async with clock, tasks:
        submitted = await job.submit()
        await _settle(job, submitted.id)
        # Act
        await clock.advance(10 * 365 * 24 * 3600)
        info = await job.get(submitted.id)

    # Assert
    assert info is not None


async def test_job_keep_never_forgets_a_running_job() -> None:
    """Retention counts from the end, so a long run stays readable."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks(shutdown_timeout=0)
    job = tasks.job(keep=0)(_wait_for)
    gate = asyncio.Event()

    async with clock, tasks:
        submitted = await job.submit(gate)
        # Act
        await clock.advance(3600)
        info = await job.get(submitted.id)
        gate.set()

    # Assert
    assert info is not None
    assert info.status is JobStatus.RUNNING


async def test_job_cancel_queued_ends_it_before_it_starts() -> None:
    """A queued job cancels at once and never runs."""
    # Arrange
    tasks = Tasks(shutdown_timeout=0)
    job = tasks.job(max_concurrent=1)(_wait_for)
    gate = asyncio.Event()

    async with tasks:
        await job.submit(gate)
        queued = await job.submit(gate)
        # Act
        cancelled = await job.cancel(queued.id)
        gate.set()
        settled = await _settle(job, queued.id)

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.finished_at is not None
    assert settled.started_at is None


async def _mark_ran(_progress: Progress, ran: list[bool]) -> None:
    """Record that the body ran."""
    ran.append(True)


@pytest.mark.parametrize("keep", [3600, 0])
async def test_job_cancel_before_its_first_step_never_runs_it(
    keep: int,
) -> None:
    """A job cancelled in the tick it was submitted ends `cancelled` unrun."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(keep=keep)(_mark_ran)
    ran: list[bool] = []

    async with tasks:
        submitted = await job.submit(ran)
        # Act
        cancelled = await job.cancel(submitted.id)
        for _ in range(5):
            await asyncio.sleep(0)
        settled = await job.get(submitted.id)

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert ran == []
    if keep:
        assert settled == cancelled
    else:
        assert settled is None


class _Load:
    """A large working set a body holds while it runs."""


async def _hold_load(
    _progress: Progress, loads: list[weakref.ref[_Load]]
) -> None:
    """Hold a `_Load` in a local and wait."""
    load = _Load()
    loads.append(weakref.ref(load))
    await asyncio.Event().wait()


async def test_job_cancel_running_frees_the_body_locals() -> None:
    """A cancelled job keeps no reference to the frames its body ran in."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_hold_load)
    loads: list[weakref.ref[_Load]] = []

    async with tasks:
        submitted = await job.submit(loads)
        await _statuses(job, [submitted.id])
        # Act
        await job.cancel(submitted.id)
        await _settle(job, submitted.id)
        await asyncio.sleep(0)
        gc.collect()

        # Assert
        assert loads[0]() is None


async def _one_then_wait(progress: Progress, gate: asyncio.Event) -> None:
    """Report one result, then run until `gate` is set."""
    progress.advance(result="first")
    await gate.wait()


async def test_job_cancel_running_stops_it_at_its_next_await() -> None:
    """`cancel` returns at once, and the body ends `cancelled`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_one_then_wait)
    gate = asyncio.Event()

    async with tasks:
        submitted = await job.submit(gate)
        await _statuses(job, [submitted.id])
        # Act
        requested = await job.cancel(submitted.id)
        settled = await _settle(job, submitted.id)

    # Assert
    assert requested is not None
    assert requested.cancel_requested is True
    assert requested.status is JobStatus.RUNNING
    assert settled.status is JobStatus.CANCELLED
    assert settled.results == ("first",)
    assert not gate.is_set()


async def _swallow_cancel(_progress: Progress, gate: asyncio.Event) -> None:
    """Catch the cancellation and return."""
    with contextlib.suppress(asyncio.CancelledError):
        await gate.wait()


async def test_job_cancel_running_ends_cancelled_when_the_body_swallows_it() -> (
    None
):
    """A body that catches the cancel and returns still ends `cancelled`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_swallow_cancel)
    gate = asyncio.Event()

    async with tasks:
        submitted = await job.submit(gate)
        await _statuses(job, [submitted.id])
        # Act
        await job.cancel(submitted.id)
        settled = await _settle(job, submitted.id)

    # Assert
    assert settled.status is JobStatus.CANCELLED


def _sync_checkpoint(
    progress: Progress, started: threading.Event, release: threading.Event
) -> None:
    """Block in a thread until `release`, then stop at the checkpoint."""
    started.set()
    release.wait()
    progress.checkpoint()
    progress.advance(result="past the checkpoint")


async def test_job_cancel_sync_stops_it_at_its_checkpoint() -> None:
    """A sync body reads `running` until its thread reaches `checkpoint()`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_sync_checkpoint)
    started, release = threading.Event(), threading.Event()

    async with tasks:
        submitted = await job.submit(started, release)
        await asyncio.to_thread(started.wait)
        # Act
        requested = await job.cancel(submitted.id)
        while_blocked = await _statuses(job, [submitted.id])
        release.set()
        settled = await _settle(job, submitted.id)

    # Assert
    assert requested is not None
    assert requested.cancel_requested is True
    assert while_blocked == ["running"]
    assert settled.status is JobStatus.CANCELLED
    assert settled.results == ()


async def test_job_cancel_finished_changes_nothing() -> None:
    """A finished job reads the same after `cancel` as before."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        submitted = await job.submit()
        finished = await _settle(job, submitted.id)
        # Act
        cancelled = await job.cancel(submitted.id)

    # Assert
    assert cancelled == finished


@pytest.mark.parametrize("submitted_by", _OWNERS)
@pytest.mark.parametrize("cancelled_by", _OWNERS)
async def test_job_cancel_reaches_a_job_only_for_its_owner(
    submitted_by: str | None, cancelled_by: str | None
) -> None:
    """`cancel` from anyone but the owner returns `None` and leaves the job."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_wait_for)
    gate = asyncio.Event()

    async with tasks:
        submitted = await _as(job, submitted_by).submit(gate)
        # Act
        cancelled = await _as(job, cancelled_by).cancel(submitted.id)
        gate.set()
        settled = await _as(job, submitted_by).get(submitted.id)
        while settled is not None and settled.finished_at is None:
            await asyncio.sleep(0)
            settled = await _as(job, submitted_by).get(submitted.id)

    # Assert
    assert settled is not None
    if cancelled_by == submitted_by:
        assert cancelled is not None
        assert settled.status is JobStatus.CANCELLED
    else:
        assert cancelled is None
        assert settled.status is JobStatus.SUCCEEDED
        assert settled.cancel_requested is False


async def test_job_cancel_returns_none_for_an_unknown_id() -> None:
    """`cancel` returns `None` for an id no job has."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act
        cancelled = await job.cancel("unknown")

    # Assert
    assert cancelled is None


def _greeting() -> str:
    """Provide a value through `Depends`."""
    return "hello"


async def _collect(
    _progress: Progress,
    sink: list[str],
    greeting: str = Depends(_greeting),
) -> None:
    """Append the injected greeting to the list it was handed."""
    sink.append(greeting)


async def test_job_submit_passes_arguments_as_they_are() -> None:
    """A submitted argument is the same object in the body, never a copy.

    A `Depends` parameter after it is still injected.
    """
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_collect)
    sink: list[str] = []

    async with tasks:
        # Act
        submitted = await job.submit(sink)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert sink == ["hello"]


async def _collect_annotated(
    _progress: Progress,
    sink: list[str],
    greeting: Annotated[str, Depends(_greeting)],
) -> None:
    """Append a greeting injected through `Annotated`."""
    sink.append(greeting)


async def test_job_submit_does_not_ask_for_an_annotated_dependency() -> None:
    """A `Depends` parameter with no default is injected, not submitted."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_collect_annotated)
    sink: list[str] = []

    async with tasks:
        # Act
        submitted = await job.submit(sink)  # type: ignore[call-arg]  # ty: ignore[missing-argument]
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert sink == ["hello"]


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        pytest.param(([], "extra"), {}, id="extra-positional"),
        pytest.param(([],), {"greeting": "hi"}, id="dependency-by-keyword"),
        pytest.param(([],), {"unknown": 1}, id="unknown-keyword"),
        pytest.param((), {}, id="missing-argument"),
    ],
)
async def test_job_submit_raises_for_arguments_the_body_does_not_take(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    """`submit` raises `TypeError` at once and records no job."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_collect)

    async with tasks:
        # Act & Assert
        with pytest.raises(TypeError, match="_collect"):
            await job.submit(*args, **kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def _report(progress: Progress, result: object) -> None:
    """Report `result`."""
    progress.advance(result=result)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(object(), id="object"),
        pytest.param({1, 2}, id="set"),
        pytest.param({"at": timedelta(seconds=1)}, id="nested-timedelta"),
    ],
)
async def test_job_advance_fails_the_job_on_a_non_json_result(
    result: object,
) -> None:
    """`advance` refuses a result a shared store could not hold."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_report)

    async with tasks:
        # Act
        submitted = await job.submit(result)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.results == ()


async def test_job_advance_keeps_json_results_as_reported() -> None:
    """Strings, numbers, booleans, lists and dicts of them are kept."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_report)
    result = {"id": "r-1", "rows": [1, 2.5, True, None], "ok": False}

    async with tasks:
        # Act
        submitted = await job.submit(result)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.results == (result,)


async def _count_with(progress: Progress, total: object, n: object) -> None:
    """Set `total`, then advance by `n`."""
    progress.total = total  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    progress.advance(n)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("total", "n"),
    [
        pytest.param(2.5, 1, id="float-total"),
        pytest.param(True, 1, id="bool-total"),
        pytest.param(2, 0.5, id="float-n"),
        pytest.param(2, True, id="bool-n"),
        pytest.param(-1, 1, id="negative-total"),
        pytest.param(2, -1, id="negative-n"),
    ],
)
async def test_job_bad_count_fails_the_job(total: object, n: object) -> None:
    """A `total` or `advance(n)` that is not a non-negative `int` fails it."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count_with)

    async with tasks:
        # Act
        submitted = await job.submit(total, n)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.done == 0


async def _report_then_change(progress: Progress) -> None:
    """Report a dict, then change it."""
    result: dict[str, JsonValue] = {"rows": [1]}
    progress.advance(result=result)
    result["rows"] = "changed"


async def test_job_advance_keeps_a_result_as_it_was_reported() -> None:
    """A result changed after `advance` reads back as it was reported."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_report_then_change)

    async with tasks:
        # Act
        submitted = await job.submit()
        info = await _settle(job, submitted.id)

    # Assert
    assert info.results == ({"rows": [1]},)


async def _count_without_results(progress: Progress, size: int) -> None:
    """Report items in one batch, with no result, and read back the counts."""
    progress.total = size
    progress.advance(size)
    if (progress.done, progress.total) != (size, size):
        raise JobError


async def test_job_advance_without_a_result_counts_items_only() -> None:
    """`advance(n)` adds `n` items done and no result."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count_without_results)

    async with tasks:
        # Act
        submitted = await job.submit(3)
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert (info.done, info.total) == (3, 3)
    assert info.results == ()


async def _unknown_total(progress: Progress) -> None:
    """Set a total, then take it back."""
    progress.total = 3
    progress.total = None


async def test_job_total_set_back_to_none_reads_unknown() -> None:
    """`progress.total = None` reads back as an unknown total."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_unknown_total)

    async with tasks:
        # Act
        submitted = await job.submit()
        info = await _settle(job, submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert info.total is None


_KICKED: dict[str, Job[[]]] = {}
"""The job `_kick` submits to."""


async def _kick() -> None:
    """Submit to the job in `_KICKED` on the task's first fire."""
    await _KICKED["job"].submit()


async def test_job_takes_a_submit_from_a_task_registered_above_it() -> None:
    """A task that submits on its first fire finds the job already started."""
    # Arrange
    tasks = Tasks()
    tasks.every(interval=60)(_kick)
    job = tasks.job()(_noop)
    _KICKED["job"] = job

    async with tasks:
        # Act
        await _statuses(job, [])
        [kick] = [task for task in tasks.tasks if task is not job]

    # Assert
    assert kick.last_fire is not None  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    assert kick.last_fire.outcome == "success"  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]


async def test_job_submit_raises_outside_the_tasks() -> None:
    """`submit` before `async with tasks` raises `OutOfContextError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    # Act & Assert
    with pytest.raises(OutOfContextError):
        await job.submit()
