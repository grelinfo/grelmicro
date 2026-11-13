"""Test on-demand jobs."""

import asyncio
import contextlib
import contextvars
import gc
import json
import logging
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict, Unpack
from uuid import UUID, uuid4

import pytest
from fast_depends import Depends
from pydantic import BaseModel, SecretStr

from grelmicro.clock import VirtualClock
from grelmicro.errors import AdmissionError, OutOfContextError
from grelmicro.security.principal import Principal
from grelmicro.task import (
    FireOutcome,
    Job,
    JobError,
    JobInfo,
    JobPage,
    JobRefusedError,
    JobStatus,
    JobView,
    Progress,
    Tasks,
)
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]


async def _settle(job: JobView[...], job_id: str) -> JobInfo:
    """Poll `job_id` until it reaches an end state, and return it."""
    while True:
        info = await job.get(job_id)
        assert info is not None
        if info.finished_at is not None:
            return info
        await asyncio.sleep(0)


async def _count(progress: Progress, items: list[str]) -> list[str]:
    """Count each item, and return them in upper case."""
    progress.total = len(items)
    for _ in items:
        progress.advance()
    return [item.upper() for item in items]


async def test_job_submit_runs_and_succeeds() -> None:
    """A submitted job runs in the background and ends `succeeded`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count)

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(["a", "b"])
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert submitted.status in {JobStatus.QUEUED, JobStatus.RUNNING}
    assert info.id == submitted.id
    assert info.name == "tests.task.test_job:_count"
    assert info.status is JobStatus.SUCCEEDED
    assert (info.done, info.total) == (2, 2)
    assert info.result == ["A", "B"]
    assert info.error is None
    assert info.cancel_requested is False
    assert info.started_at is not None
    assert info.finished_at is not None
    assert info.created_at <= info.started_at <= info.finished_at


async def _fail_after_one(progress: Progress, message: str | None) -> None:
    """Report one result, then raise a `JobError` or a plain error."""
    progress.advance()
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
async def test_job_failure_keeps_its_counts_and_has_no_result(
    message: str | None, shown: str
) -> None:
    """A body that raises ends `failed`, with only safe text in `error`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_fail_after_one)

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(message)
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.error == shown
    assert info.result is None
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
        submitted = await job.without_owner().submit(message)
        await _settle(job.without_owner(), submitted.id)

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
        submitted = await job.without_owner().submit(mixed)
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.error == shown
    [record] = [r for r in caplog.records if r.name == "grelmicro.task"]
    assert record.levelno == level
    assert (record.exc_info is None) is (not mixed)
    assert ("Row 3 is invalid" in record.getMessage()) is (not mixed)


def _count_in_thread(progress: Progress, items: list[str]) -> str:
    """Report one result per item from a worker thread."""
    progress.total = len(items)
    for _ in items:
        progress.advance()
    return threading.current_thread().name


async def test_job_sync_body_runs_in_a_worker_thread() -> None:
    """A sync body runs off the event loop and reports like an async one."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_count_in_thread)

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(["a"])
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert info.done == 1
    assert info.result != threading.current_thread().name


async def _noop(progress: Progress) -> None:
    """Do nothing."""


_OWNERS = [None, "alice", "bob"]


def _as[**P](job: Job[P], owner: str | None) -> JobView[P]:
    """Return `job` as `owner` sees it, or for no owner."""
    return job.without_owner() if owner is None else job.for_owner(owner)


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
        info = await job.without_owner().get("unknown")

    # Assert
    assert info is None


@pytest.mark.parametrize("method", ["submit", "get", "cancel", "list"])
def test_job_needs_a_choice_of_owner_first(method: str) -> None:
    """`Job` has no `submit`, `get`, `cancel` or `list` of its own."""
    # Arrange
    job = Tasks().job()(_noop)

    # Act
    found = hasattr(job, method)

    # Assert
    assert not found


@dataclass(frozen=True)
class _Caller:
    """A caller, as an authentication layer leaves it."""

    subject: str | None = "alice"
    issuer: str | None = "https://idp.example"
    scopes: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)
    is_authenticated: bool = True


@pytest.mark.parametrize("owner", [None, 123, b"alice", object()])
def test_job_for_owner_refuses_what_cannot_own_a_job(
    owner: Any,  # noqa: ANN401
) -> None:
    """`for_owner` raises `TypeError` for anything but a string or a caller."""
    # Arrange
    job = Tasks().job()(_noop)

    # Act / Assert
    with pytest.raises(TypeError, match="for_owner"):
        job.for_owner(owner)


@pytest.mark.parametrize(
    "owner",
    [
        "",
        _Caller(subject=None),
        _Caller(subject=""),
        _Caller(is_authenticated=False),
    ],
)
def test_job_for_owner_refuses_an_owner_that_names_nobody(
    owner: str | Principal,
) -> None:
    """An empty owner, or a caller naming nobody, raises `ValueError`."""
    # Arrange
    job = Tasks().job()(_noop)

    # Act / Assert
    with pytest.raises(ValueError, match="for_owner"):
        job.for_owner(owner)


async def test_job_for_owner_finds_a_job_for_the_same_caller() -> None:
    """Two equal callers are the same owner, on another request too."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        submitted = await job.for_owner(_Caller()).submit()
        # Act
        info = await job.for_owner(_Caller()).get(submitted.id)

    # Assert
    assert info is not None


async def test_job_for_owner_accepts_a_caller_with_no_issuer() -> None:
    """A caller with no issuer owns a job by its subject alone."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)
    caller = _Caller(issuer=None)

    async with tasks:
        submitted = await job.for_owner(caller).submit()
        # Act
        info = await job.for_owner(caller).get(submitted.id)

    # Assert
    assert info is not None


@pytest.mark.parametrize(
    ("owner", "other"),
    [
        (_Caller(subject="alice"), _Caller(subject="bob")),
        (_Caller(issuer="https://a"), _Caller(issuer="https://b")),
        (
            _Caller(issuer="a:b", subject="c"),
            _Caller(issuer="a", subject="b:c"),
        ),
        (_Caller(issuer=None), _Caller(issuer="")),
        (_Caller(issuer=None), "alice"),
    ],
)
async def test_job_for_owner_keeps_callers_apart(
    owner: str | Principal, other: str | Principal
) -> None:
    """Issuer and subject together name the owner, with no way to collide."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        submitted = await job.for_owner(owner).submit()
        # Act
        info = await job.for_owner(other).get(submitted.id)

    # Assert
    assert info is None


async def _wait_for(_progress: Progress, gate: str) -> None:
    """Run until the shared `gate` is set."""
    await shared(gate).wait()


async def _statuses(job: Job[...], ids: list[str]) -> list[JobStatus]:
    """Return the status of each job in `ids`, after the loop settles."""
    for _ in range(5):
        await asyncio.sleep(0)
    infos = [await job.without_owner().get(job_id) for job_id in ids]
    return [info.status for info in infos if info is not None]


_USER: contextvars.ContextVar[str] = contextvars.ContextVar("user")
"""Who submitted the job, as a request context would carry it."""


async def _record_user(_progress: Progress, seen: str, gate: str) -> None:
    """Record the submitter the body sees, then wait for the shared `gate`."""
    shared(seen).append(_USER.get("nobody"))
    await shared(gate).wait()


async def _submit_as(
    job: Job[[str, str]],
    user: str,
    seen: list[str],
    gate: asyncio.Event,
) -> str:
    """Submit from a context where `user` is the submitter."""
    _USER.set(user)
    return (await job.without_owner().submit(share(seen), share(gate))).id


async def test_job_body_never_sees_the_context_of_its_submitter() -> None:
    """A body runs in the app's context, wherever it runs and whoever asked."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=1)(_record_user)
    seen: list[str] = []
    gate = asyncio.Event()
    token = _USER.set("app")

    try:
        async with tasks:
            # Act
            await asyncio.create_task(_submit_as(job, "alice", seen, gate))
            bob = await asyncio.create_task(_submit_as(job, "bob", seen, gate))
            gate.set()
            await _settle(job.without_owner(), bob)
    finally:
        _USER.reset(token)

    # Assert
    assert seen == ["app", "app"]


async def test_job_max_concurrent_queues_the_rest_in_order() -> None:
    """Past `max_concurrent`, jobs wait and then start in submission order."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=2)(_wait_for)
    gates = [asyncio.Event() for _ in range(4)]

    async with tasks:
        ids = [
            (await job.without_owner().submit(share(gate))).id for gate in gates
        ]
        # Act
        before = await _statuses(job, ids)
        gates[0].set()
        after_one = await _statuses(job, ids)
        for gate in gates:
            gate.set()
        settled = [await _settle(job.without_owner(), job_id) for job_id in ids]

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
                await job.without_owner().submit(share(gate))
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
        submitted = await job.without_owner().submit()
        for _ in range(5):
            await asyncio.sleep(0)
        # Act
        at_finish = await job.without_owner().get(submitted.id)
        if kept_for is not None:
            await clock.advance(kept_for - 1)
        just_before = await job.without_owner().get(submitted.id)
        await clock.advance(1)
        after = await job.without_owner().get(submitted.id)

    # Assert
    if kept_for is None:
        assert at_finish is None
    else:
        assert at_finish is not None
        assert at_finish.status is JobStatus.SUCCEEDED
        assert just_before is not None
    assert after is None


async def _advance_in_steps(
    clock: VirtualClock, seconds: int, step: int = 1
) -> None:
    """Move time forward in steps, so a replica's beats and sweeps run."""
    for _ in range(seconds // step):
        await clock.advance(step)
        for _ in range(3):
            await asyncio.sleep(0)


async def test_job_keep_frees_an_expired_job_with_no_call_after_it() -> None:
    """A job past `keep` leaves memory even when nothing reads the job."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(keep=60)(_noop)

    async with clock, tasks:
        await job.without_owner().submit()
        for _ in range(5):
            await asyncio.sleep(0)
        rows = job._backend._rows  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        kept = len(rows)
        # Act
        await _advance_in_steps(clock, 70)
        left = len(rows)

    # Assert
    assert (kept, left) == (1, 0)


async def test_job_keep_none_keeps_a_finished_job() -> None:
    """`keep=None` keeps a finished job readable with no time limit."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(keep=None)(_noop)

    async with clock, tasks:
        submitted = await job.without_owner().submit()
        await _settle(job.without_owner(), submitted.id)
        # Act
        await clock.advance(10 * 365 * 24 * 3600)
        info = await job.without_owner().get(submitted.id)

    # Assert
    assert info is not None


@pytest.mark.parametrize(
    ("succeeded", "failed", "kept"),
    [
        pytest.param(1, None, [False, True], id="capped"),
        pytest.param(None, 1, [True, True], id="other-kind"),
        pytest.param(None, None, [True, True], id="no-cap"),
    ],
)
async def test_job_max_kept_reaches_the_store(
    succeeded: int | None, failed: int | None, kept: list[bool]
) -> None:
    """A job's caps apply to the runs it stores."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_kept_succeeded=succeeded, max_kept_failed=failed)(_noop)
    view = job.without_owner()

    async with tasks:
        first = await view.submit()
        await _settle(view, first.id)
        # Act
        second = await view.submit()
        await _settle(view, second.id)
        read = [await view.get(first.id), await view.get(second.id)]

    # Assert
    assert [info is not None for info in read] == kept


async def test_job_keep_never_forgets_a_running_job() -> None:
    """Retention counts from the end, so a long run stays readable."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks(shutdown_timeout=0)
    job = tasks.job(keep=0)(_wait_for)
    gate = asyncio.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        # Act
        await _advance_in_steps(clock, 3600, step=10)
        info = await job.without_owner().get(submitted.id)
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
        await job.without_owner().submit(share(gate))
        queued = await job.without_owner().submit(share(gate))
        # Act
        cancelled = await job.without_owner().cancel(queued.id)
        gate.set()
        settled = await _settle(job.without_owner(), queued.id)

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.finished_at is not None
    assert settled.started_at is None


async def _mark_ran(_progress: Progress, ran: str) -> None:
    """Record that the body ran."""
    shared(ran).append(True)


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
        submitted = await job.without_owner().submit(share(ran))
        # Act
        cancelled = await job.without_owner().cancel(submitted.id)
        for _ in range(5):
            await asyncio.sleep(0)
        settled = await job.without_owner().get(submitted.id)

    # Assert
    assert cancelled is not None
    assert cancelled.cancel_requested is True
    assert ran == []
    if keep:
        assert settled is not None
        assert settled.status is JobStatus.CANCELLED
    else:
        assert settled is None


class _Load:
    """A large working set a body holds while it runs."""


async def _hold_load(_progress: Progress, loads: str) -> None:
    """Hold a `_Load` in a local and wait."""
    load = _Load()
    shared(loads).append(weakref.ref(load))
    await asyncio.Event().wait()


async def test_job_cancel_running_frees_the_body_locals() -> None:
    """A cancelled job keeps no reference to the frames its body ran in."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_hold_load)
    loads: list[weakref.ref[_Load]] = []

    async with tasks:
        submitted = await job.without_owner().submit(share(loads))
        await _statuses(job, [submitted.id])
        # Act
        await job.without_owner().cancel(submitted.id)
        await _settle(job.without_owner(), submitted.id)
        await asyncio.sleep(0)
        gc.collect()

        # Assert
        assert loads[0]() is None


async def _one_then_wait(progress: Progress, gate: str) -> None:
    """Count one item, then run until the shared `gate` is set."""
    progress.advance()
    await shared(gate).wait()


async def test_job_cancel_running_stops_it_at_its_next_await() -> None:
    """`cancel` returns at once, and the body ends `cancelled`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_one_then_wait)
    gate = asyncio.Event()

    async with tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _statuses(job, [submitted.id])
        # Act
        requested = await job.without_owner().cancel(submitted.id)
        settled = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert requested is not None
    assert requested.cancel_requested is True
    assert requested.status is JobStatus.RUNNING
    assert settled.status is JobStatus.CANCELLED
    assert settled.result is None
    assert settled.done == 1
    assert not gate.is_set()


async def _swallow_cancel(_progress: Progress, gate: str) -> None:
    """Catch the cancellation and return."""
    with contextlib.suppress(asyncio.CancelledError):
        await shared(gate).wait()


async def test_job_cancel_running_ends_cancelled_when_the_body_swallows_it() -> (
    None
):
    """A body that catches the cancel and returns still ends `cancelled`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_swallow_cancel)
    gate = asyncio.Event()

    async with tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _statuses(job, [submitted.id])
        # Act
        await job.without_owner().cancel(submitted.id)
        settled = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert settled.status is JobStatus.CANCELLED


def _sync_checkpoint(progress: Progress, started: str, release: str) -> None:
    """Block in a thread until `release`, then stop at the checkpoint."""
    shared(started).set()
    shared(release).wait()
    progress.checkpoint()
    progress.advance()


async def test_job_cancel_sync_stops_it_at_its_checkpoint() -> None:
    """A sync body reads `running` until its thread reaches `checkpoint()`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_sync_checkpoint)
    started, release = threading.Event(), threading.Event()

    async with tasks:
        submitted = await job.without_owner().submit(
            share(started), share(release)
        )
        await asyncio.to_thread(started.wait)
        # Act
        requested = await job.without_owner().cancel(submitted.id)
        while_blocked = await _statuses(job, [submitted.id])
        release.set()
        settled = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert requested is not None
    assert requested.cancel_requested is True
    assert while_blocked == ["running"]
    assert settled.status is JobStatus.CANCELLED
    assert settled.result is None
    assert settled.done == 0


async def test_job_cancel_finished_changes_nothing() -> None:
    """A finished job reads the same after `cancel` as before."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        submitted = await job.without_owner().submit()
        finished = await _settle(job.without_owner(), submitted.id)
        # Act
        cancelled = await job.without_owner().cancel(submitted.id)

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
        submitted = await _as(job, submitted_by).submit(share(gate))
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
        cancelled = await job.without_owner().cancel("unknown")

    # Assert
    assert cancelled is None


def _greeting() -> str:
    """Provide a value through `Depends`."""
    return "hello"


async def _collect(
    _progress: Progress,
    sink: str,
    greeting: str = Depends(_greeting),
) -> None:
    """Append the injected greeting to the list it was handed."""
    shared(sink).append(greeting)


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
        submitted = await job.without_owner().submit(share(sink))
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert sink == ["hello"]


async def _collect_annotated(
    _progress: Progress,
    sink: str,
    greeting: Annotated[str, Depends(_greeting)],
) -> None:
    """Append a greeting injected through `Annotated`."""
    shared(sink).append(greeting)


async def test_job_submit_does_not_ask_for_an_annotated_dependency() -> None:
    """A `Depends` parameter with no default is injected, not submitted."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_collect_annotated)
    sink: list[str] = []

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(share(sink))  # type: ignore[call-arg]  # ty: ignore[missing-argument]
        info = await _settle(job.without_owner(), submitted.id)

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
            await job.without_owner().submit(*args, **kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def _report(_progress: Progress, result: str) -> object:
    """Return the shared `result`."""
    return shared(result)


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(object(), id="object"),
        pytest.param({1, 2}, id="set"),
        pytest.param({"at": timedelta(seconds=1)}, id="nested-timedelta"),
    ],
)
async def test_job_return_fails_the_job_on_a_non_json_result(
    result: object,
) -> None:
    """A body that returns something a store cannot hold ends `failed`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_report)

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(share(result))
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.result is None


async def test_job_return_keeps_a_json_result_as_returned() -> None:
    """Strings, numbers, booleans, lists and dicts of them are kept."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_report)
    result = {"id": "r-1", "rows": [1, 2.5, True, None], "ok": False}

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(share(result))
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.result == result


async def _count_with(progress: Progress, total: str, n: str) -> None:
    """Set `total`, then advance by `n`."""
    progress.total = shared(total)
    progress.advance(shared(n))


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
        submitted = await job.without_owner().submit(share(total), share(n))
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.done == 0


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
        submitted = await job.without_owner().submit(3)
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert (info.done, info.total) == (3, 3)
    assert info.result is None


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
        submitted = await job.without_owner().submit()
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert info.total is None


_KICKED: dict[str, Job[[]]] = {}
"""The job `_kick` submits to."""


async def _kick() -> None:
    """Submit to the job in `_KICKED` on the task's first fire."""
    await _KICKED["job"].without_owner().submit()


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


class _Upload(BaseModel):
    """A model passed as an argument."""

    name: str


async def _record_arguments(
    _progress: Progress,
    sink: str,
    when: datetime,
    upload_id: UUID,
    upload: _Upload,
    rows: list[int],
    plain=None,  # noqa: ANN001
    *extra: int,
    **more: str,
) -> None:
    """Record each argument as the body receives it."""
    shared(sink).extend([when, upload_id, upload, rows, plain, extra, more])


async def test_job_submit_rebuilds_arguments_from_the_type_hints() -> None:
    """A typed argument arrives as that type, as a copy."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_record_arguments)
    sink: list[object] = []
    when = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    upload_id, upload, rows = uuid4(), _Upload(name="a"), [1, 2]

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(
            share(sink),
            when,
            upload_id,
            upload,
            rows,
            {"k": [1]},
            3,
            4,
            tag="x",
        )
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.SUCCEEDED
    assert sink == [
        when,
        upload_id,
        upload,
        rows,
        {"k": [1]},
        (3, 4),
        {"tag": "x"},
    ]
    assert isinstance(sink[0], datetime)
    assert isinstance(sink[1], UUID)
    assert isinstance(sink[2], _Upload)
    assert sink[2] is not upload
    assert sink[3] is not rows


@pytest.mark.parametrize(
    ("args", "named"),
    [
        pytest.param(("x", "not-a-date"), "when", id="wrong-type"),
        pytest.param(
            ("x", "2026-11-04T09:00:00"), "when", id="strict-no-coercion"
        ),
    ],
)
async def test_job_submit_refuses_an_argument_of_the_wrong_type(
    args: tuple[str, str], named: str
) -> None:
    """A value that does not match its type hint raises, naming the parameter."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_record_arguments)
    rest = (uuid4(), _Upload(name="a"), [1])

    async with tasks:
        # Act & Assert
        with pytest.raises(TypeError, match=named):
            await job.without_owner().submit(*args, *rest)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def _take_anything(_progress: Progress, value) -> None:  # noqa: ANN001
    """Take one argument with no type hint."""


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(object(), id="object"),
        pytest.param({1, 2}, id="set"),
        pytest.param({"at": timedelta(seconds=1)}, id="nested-timedelta"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
    ],
)
async def test_job_submit_refuses_an_untyped_argument_that_is_not_json(
    value: object,
) -> None:
    """With no type hint, an argument must be plain JSON."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_take_anything)

    async with tasks:
        # Act & Assert
        with pytest.raises(TypeError, match="value"):
            await job.without_owner().submit(value)


async def test_job_submit_refuses_a_value_that_validates_but_has_no_json_form() -> (
    None
):
    """A typed value that cannot be dumped to JSON raises at `submit`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_take_any_typed)

    async with tasks:
        # Act & Assert
        with pytest.raises(TypeError, match="value"):
            await job.without_owner().submit(object())


async def _take_any_typed(_progress: Progress, value: Any) -> None:  # noqa: ANN401
    """Take an argument typed as anything."""


async def _count_then_wait(progress: Progress, gate: str) -> None:
    """Report counts, then run until the shared `gate` is set."""
    progress.total = 3
    progress.advance(2)
    await shared(gate).wait()


async def test_job_progress_reaches_a_read_within_a_beat() -> None:
    """Counts reach a poll after a second, while the body still runs."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_count_then_wait)
    gate = asyncio.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _statuses(job, [submitted.id])
        # Act
        before = await job.without_owner().get(submitted.id)
        await _advance_in_steps(clock, 2)
        after = await job.without_owner().get(submitted.id)
        gate.set()

    # Assert
    assert before is not None
    assert (before.done, before.total) == (0, None)
    assert after is not None
    assert after.status is JobStatus.RUNNING
    assert (after.done, after.total) == (2, 3)


async def test_job_cancel_from_another_replica_reaches_the_body() -> None:
    """A cancel written to the store reaches the running body on a beat."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_wait_for)

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(asyncio.Event()))
        await _statuses(job, [submitted.id])
        # Act
        await job._backend.cancel(
            name=job.name, job_id=submitted.id, owner=None
        )
        await _advance_in_steps(clock, 2)
        settled = await job.without_owner().get(submitted.id)

    # Assert
    assert settled is not None
    assert settled.status is JobStatus.CANCELLED


async def test_job_lease_lost_stops_the_body_and_reads_lost() -> None:
    """A replica frozen past its lease stops its body, and the job reads lost."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_wait_for)

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(asyncio.Event()))
        await _statuses(job, [submitted.id])
        # Act
        await clock.advance(31)
        await _advance_in_steps(clock, 2)
        settled = await job.without_owner().get(submitted.id)

    # Assert
    assert settled is not None
    assert settled.status is JobStatus.FAILED
    assert settled.error == "Job lost"


async def test_job_max_wait_ends_a_job_that_never_got_a_slot() -> None:
    """A queued job past `max_wait` reads `failed`."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_concurrent=1, max_wait=60)(_wait_for)
    gate = asyncio.Event()

    async with clock, tasks:
        await job.without_owner().submit(share(gate))
        queued = await job.without_owner().submit(share(gate))
        # Act
        await _advance_in_steps(clock, 61)
        read = await job.without_owner().get(queued.id)
        gate.set()

    # Assert
    assert read is not None
    assert read.status is JobStatus.FAILED
    assert read.error == "Job not started in time"


async def test_job_max_run_cancels_an_async_body_and_reads_failed() -> None:
    """An async body past `max_run` stops at its next `await`, `failed`."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_run=5)(_one_then_wait)
    gate = asyncio.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _statuses(job, [submitted.id])
        # Act
        await _advance_in_steps(clock, 7)
        settled = await job.without_owner().get(submitted.id)
        running = job._runs.copy()

    # Assert
    assert settled is not None
    assert settled.status is JobStatus.FAILED
    assert settled.error == "Job not finished in time"
    assert settled.done == 1
    assert running == {}
    assert not gate.is_set()


async def test_job_max_run_none_lets_a_body_run_on() -> None:
    """With no `max_run`, a body runs as long as its beats arrive."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_wait_for)
    gate = asyncio.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _statuses(job, [submitted.id])
        # Act
        await _advance_in_steps(clock, 70)
        read = await job.without_owner().get(submitted.id)
        gate.set()

    # Assert
    assert read is not None
    assert read.status is JobStatus.RUNNING


def _sync_checkpoint_loop(progress: Progress, started: str) -> None:
    """Report one item, then stop only at a checkpoint."""
    progress.advance()
    shared(started).set()
    while True:
        progress.checkpoint()
        time.sleep(0.001)


async def test_job_max_run_stops_a_sync_body_at_its_checkpoint() -> None:
    """A sync body past `max_run` stops at its next `checkpoint()`."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_run=5)(_sync_checkpoint_loop)
    started = threading.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(started))
        await asyncio.to_thread(started.wait)
        # Act
        await _advance_in_steps(clock, 7)
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0.001)
        settled = await job.without_owner().get(submitted.id)

    # Assert
    assert settled is not None
    assert settled.status is JobStatus.FAILED
    assert settled.error == "Job not finished in time"
    assert settled.done == 1


def _sync_advance_loop(progress: Progress, started: str) -> None:
    """Report items until stopped, with no checkpoint."""
    progress.advance()
    shared(started).set()
    while True:
        progress.advance()
        time.sleep(0.001)


async def test_job_max_run_stops_a_sync_body_at_its_next_advance() -> None:
    """A sync body past `max_run` stops at its next `advance()` too."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_run=5)(_sync_advance_loop)
    started = threading.Event()

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(started))
        await asyncio.to_thread(started.wait)
        # Act
        await _advance_in_steps(clock, 7)
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0.001)
        settled = await job.without_owner().get(submitted.id)

    # Assert
    assert settled is not None
    assert settled.status is JobStatus.FAILED
    assert settled.error == "Job not finished in time"


async def test_job_max_run_counts_from_the_claim() -> None:
    """A queued job's `max_run` starts when it starts, not when submitted."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_run=5)(_wait_for)
    first_gate, second_gate = asyncio.Event(), asyncio.Event()

    async with clock, tasks:
        first = await job.without_owner().submit(share(first_gate))
        second = await job.without_owner().submit(share(second_gate))
        # Act
        await _advance_in_steps(clock, 7)
        under = await job.without_owner().get(second.id)
        await _advance_in_steps(clock, 5)
        past = await job.without_owner().get(second.id)
        lapsed = await job.without_owner().get(first.id)

    # Assert
    assert lapsed is not None
    assert lapsed.error == "Job not finished in time"
    assert under is not None
    assert under.status is JobStatus.RUNNING
    assert past is not None
    assert past.error == "Job not finished in time"


async def _takes_a_number(_progress: Progress, number: int) -> None:
    """Take an argument with a type hint."""


async def test_job_stored_arguments_that_no_longer_match_end_failed() -> None:
    """A job stored by older code ends `failed`, and its body never runs."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_takes_a_number)

    async with tasks:
        # Act: a replica on older code stored another shape.
        await job._backend.submit(
            name=job.name,
            job_id="old",
            owner=None,
            payload='{"number": "not a number"}',
            context={},
            max_queued=None,
            max_wait=None,
            max_run=None,
            keep=None,
            max_kept_succeeded=None,
            max_kept_failed=None,
            start_as=None,
            lease=timedelta(seconds=30),
        )
        job._wake_loop()
        settled = await _settle(job.without_owner(), "old")

    # Assert
    assert settled.status is JobStatus.FAILED
    assert settled.error == "Job failed"


async def test_job_cancel_twice_cancels_the_body_once() -> None:
    """A second cancel of a running job changes nothing."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_wait_for)

    async with tasks:
        submitted = await job.without_owner().submit(share(asyncio.Event()))
        await _statuses(job, [submitted.id])
        first = await job.without_owner().cancel(submitted.id)
        # Act
        second = await job.without_owner().cancel(submitted.id)
        settled = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert first is not None
    assert second is not None
    assert second.cancel_requested is True
    assert settled.status is JobStatus.CANCELLED


async def test_job_free_slot_never_jumps_an_older_queued_job() -> None:
    """A job already queued in the store starts before a new submit."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=1)(_record_order)
    order: list[str] = []
    key = share(order)

    async with tasks:
        await job._backend.submit(
            name=job.name,
            job_id="older",
            owner=None,
            payload=json.dumps({"order": key, "mark": "older"}),
            context={},
            max_queued=None,
            max_wait=None,
            max_run=None,
            keep=None,
            max_kept_succeeded=None,
            max_kept_failed=None,
            start_as=None,
            lease=timedelta(seconds=30),
        )
        # Act
        newer = await job.without_owner().submit(key, "newer")
        await _settle(job.without_owner(), newer.id)

    # Assert
    assert newer.status is JobStatus.QUEUED
    assert order == ["older", "newer"]


async def _record_order(_progress: Progress, order: str, mark: str) -> None:
    """Record which job ran."""
    shared(order).append(mark)


async def _take_a_pair(
    _progress: Progress, sink: str, pair: tuple[int, int]
) -> None:
    """Record a tuple argument."""
    shared(sink).append(pair)


async def test_job_submit_rebuilds_a_tuple_argument() -> None:
    """A tuple travels as a JSON list, and arrives as a tuple."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_take_a_pair)
    sink: list[tuple[int, int]] = []

    async with tasks:
        # Act
        submitted = await job.without_owner().submit(share(sink), (1, 2))
        await _settle(job.without_owner(), submitted.id)

    # Assert
    assert sink == [(1, 2)]


async def test_job_submit_raises_outside_the_tasks() -> None:
    """`submit` before `async with tasks` raises `OutOfContextError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    # Act & Assert
    with pytest.raises(OutOfContextError):
        await job.without_owner().submit()


async def test_job_idle_beat_leaves_every_slot_free() -> None:
    """A beat that claims nothing does not use up a slot."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_concurrent=1)(_count)

    async with clock, tasks:
        await _advance_in_steps(clock, 2)

        # Act
        submitted = await job.without_owner().submit([])

    # Assert
    assert submitted.status is JobStatus.RUNNING


async def test_job_beat_claims_a_job_another_replica_queued() -> None:
    """A job put in the store by another replica starts on the next beat."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_count)

    async with clock, tasks:
        queued = await job._backend.submit(
            name=job.name,
            job_id="from-another-replica",
            owner=None,
            payload='{"items": ["a"]}',
            context={},
            max_queued=None,
            max_wait=None,
            max_run=None,
            keep=None,
            max_kept_succeeded=None,
            max_kept_failed=None,
            start_as=None,
            lease=timedelta(seconds=30),
        )
        assert queued.record is not None

        # Act
        await _advance_in_steps(clock, 2)

    # Assert
    info = await job.without_owner().get("from-another-replica")
    assert info is not None
    assert info.result == ["A"]


async def test_job_sweep_reaps_and_purges_on_its_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tenth beat ends what is overdue and forgets what expired."""
    # Arrange
    calls: list[str] = []

    async def _reap(_self: object, *, limit: int) -> int:
        calls.append(f"reap {limit}")
        return 0

    async def _purge(_self: object, *, limit: int) -> int:
        calls.append(f"purge {limit}")
        return 0

    monkeypatch.setattr(MemoryJobAdapter, "reap", _reap)
    monkeypatch.setattr(MemoryJobAdapter, "purge", _purge)
    clock = VirtualClock()
    tasks = Tasks()
    tasks.job()(_count)

    # Act
    async with clock, tasks:
        await _advance_in_steps(clock, 9)
        before = list(calls)
        await _advance_in_steps(clock, 1)

    # Assert
    assert before == []
    assert calls == ["reap 100", "purge 100"]


async def test_job_exit_stops_the_beat() -> None:
    """Leaving the tasks leaves no task of the job behind."""
    # Arrange
    tasks = Tasks()
    tasks.job()(_count)
    before = asyncio.all_tasks()

    # Act
    async with tasks:
        pass
    await asyncio.sleep(0)

    # Assert
    assert asyncio.all_tasks() == before


async def _returns_nan(_progress: Progress) -> float:
    """Return a float with no JSON form."""
    return float("nan")


async def _returns_an_object(_progress: Progress) -> object:
    """Return something that is not JSON."""
    return object()


@pytest.mark.parametrize("body", [_returns_nan, _returns_an_object])
async def test_job_body_returning_no_json_is_a_failed_run(
    body: Callable[..., Any],
) -> None:
    """A return with no JSON form ends `failed`, and records a failed run."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(body)

    # Act
    async with tasks:
        submitted = await job.without_owner().submit()
        info = await _settle(job.without_owner(), submitted.id)

    # Assert
    assert info.status is JobStatus.FAILED
    assert info.error == "Job failed"
    assert job._fire.last is not None
    assert job._fire.last.outcome is FireOutcome.ERROR


async def _takes_an_int(_progress: Progress, count: int) -> None:
    """Take an int."""


async def test_job_stored_arguments_that_no_longer_match_stay_out_of_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Arguments the body refuses end `failed`, and are not logged."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_takes_an_int)
    caplog.set_level(logging.WARNING, logger="grelmicro.task")

    async with clock, tasks:
        await job._backend.submit(
            name=job.name,
            job_id="stale",
            owner=None,
            payload='{"count": "a-stored-secret"}',
            context={},
            max_queued=None,
            max_wait=None,
            max_run=None,
            keep=None,
            max_kept_succeeded=None,
            max_kept_failed=None,
            start_as=None,
            lease=timedelta(seconds=30),
        )

        # Act
        await _advance_in_steps(clock, 2)

    # Assert
    info = await job.without_owner().get("stale")
    assert info is not None
    assert info.status is JobStatus.FAILED
    assert "a-stored-secret" not in caplog.text
    assert "int_parsing" in caplog.text


class _Base(BaseModel):
    a: int


class _Sub(_Base):
    b: int


class _HoldsASecret(BaseModel):
    token: SecretStr


async def _takes_a_secret(_progress: Progress, value: SecretStr) -> None:
    """Take a secret."""


async def _takes_a_model_secret(
    _progress: Progress, value: _HoldsASecret
) -> None:
    """Take a model holding a secret."""


async def _takes_time_or_text(
    _progress: Progress, value: datetime | str
) -> None:
    """Take a union a JSON string cannot tell apart."""


async def _takes_a_base(_progress: Progress, value: _Base) -> None:
    """Take a base model."""


@pytest.mark.parametrize(
    ("body", "value"),
    [
        pytest.param(_takes_a_secret, SecretStr("real"), id="secret"),
        pytest.param(
            _takes_a_model_secret,
            _HoldsASecret(token=SecretStr("real")),
            id="model-secret",
        ),
        pytest.param(
            _takes_time_or_text, datetime.now(UTC), id="union-loses-the-type"
        ),
        pytest.param(
            _takes_a_base, _Sub(a=1, b=2), id="subclass-loses-a-field"
        ),
    ],
)
async def test_job_submit_refuses_a_value_that_would_not_arrive_the_same(
    body: Callable[..., Any], value: object
) -> None:
    """A value JSON would change raises `TypeError`, never a changed value."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(body)

    # Act & Assert
    async with tasks:
        with pytest.raises(TypeError, match="does not fit"):
            await job.without_owner().submit(value)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError, id="error"),
        pytest.param(asyncio.CancelledError, id="cancel"),
    ],
)
async def test_job_submit_that_fails_in_the_store_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    """A `submit` the store did not take leaves the slot for the next job."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)
    real = MemoryJobAdapter.submit

    async with tasks:
        with monkeypatch.context() as failing:

            async def _fails(_self: object, **_kwargs: object) -> None:
                raise error

            failing.setattr(MemoryJobAdapter, "submit", _fails)
            with pytest.raises(error):
                await job.without_owner().submit()
        assert MemoryJobAdapter.submit is real

        # Act
        info = await job.without_owner().submit()

        # Assert
        assert info.status is JobStatus.RUNNING


async def _reserved_keywords(
    progress: Progress,
    *,
    self: int,
    delay: int,
    started_at: int,
    args: int,
    kwargs: int,
    name: int,
    owner: int,
) -> int:
    """Take keywords that the job's own methods also use."""
    _ = progress
    return self + delay + started_at + args + kwargs + name + owner


def _reserved_keywords_sync(
    progress: Progress,
    *,
    self: int,
    delay: int,
    started_at: int,
    args: int,
    kwargs: int,
    name: int,
    owner: int,
) -> int:
    """Take the same keywords in a sync body."""
    _ = progress
    return self + delay + started_at + args + kwargs + name + owner


_RESERVED = {
    "self": 1,
    "delay": 1,
    "started_at": 1,
    "args": 1,
    "kwargs": 1,
    "name": 1,
    "owner": 1,
}


@pytest.mark.parametrize(
    "function", [_reserved_keywords, _reserved_keywords_sync]
)
@pytest.mark.parametrize("owned", [False, True], ids=["no-owner", "owner"])
async def test_job_body_can_use_any_keyword(
    function: Callable[..., Any],
    owned: bool,  # noqa: FBT001
) -> None:
    """No keyword of a body collides with the job's own parameters."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(function)
    target = job.for_owner("alice") if owned else job.without_owner()

    # Act
    async with tasks:
        submitted = await target.submit(**_RESERVED)
        info = await _settle(target, submitted.id)

    # Assert
    assert (info.status, info.result) == (JobStatus.SUCCEEDED, 7)


class _Options(TypedDict):
    """Keyword arguments a body declares by name."""

    count: int
    when: datetime


async def _unpacked_keywords(
    progress: Progress, **options: Unpack[_Options]
) -> list[str]:
    """Report what its keyword arguments arrive as."""
    _ = progress
    return [type(options["count"]).__name__, type(options["when"]).__name__]


async def _unpacked_positionals(
    progress: Progress,
    *row: Unpack[tuple[int, datetime]],  # noqa: UP044
) -> list[str]:
    """Report what its positional arguments arrive as."""
    _ = progress
    return [type(value).__name__ for value in row]


async def _starred_positionals(
    progress: Progress, *row: *tuple[int, datetime]
) -> list[str]:
    """Report what its positional arguments arrive as, with the star form."""
    _ = progress
    return [type(value).__name__ for value in row]


async def test_job_unpacked_keywords_arrive_as_their_types() -> None:
    """`**options: Unpack[TD]` rebuilds each keyword from the `TypedDict`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_unpacked_keywords)
    when = datetime(2026, 1, 1, tzinfo=UTC)

    # Act
    async with tasks:
        submitted = await job.without_owner().submit(count=1, when=when)
        info = await _settle(job.without_owner(), submitted.id)
        with pytest.raises(TypeError, match="does not fit"):
            await job.without_owner().submit(count="one", when=when)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert info.result == ["int", "datetime"]


@pytest.mark.parametrize(
    "function", [_unpacked_positionals, _starred_positionals]
)
async def test_job_unpacked_positionals_arrive_as_their_types(
    function: Callable[..., Any],
) -> None:
    """`*row: Unpack[tuple[...]]` rebuilds each value from its position."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(function)
    when = datetime(2026, 1, 1, tzinfo=UTC)

    # Act
    async with tasks:
        submitted = await job.without_owner().submit(1, when)
        info = await _settle(job.without_owner(), submitted.id)
        with pytest.raises(TypeError, match="does not fit"):
            await job.without_owner().submit(1)

    # Assert
    assert info.result == ["int", "datetime"]


async def test_job_list_returns_a_page_of_the_owner_s_jobs() -> None:
    """`list` returns the owner's jobs newest first, and `next` pages on."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        alice = job.for_owner("alice")
        ids = [(await alice.submit()).id for _ in range(3)]
        await job.for_owner("bob").submit()
        await job.without_owner().submit()
        # Act
        first = await alice.list(limit=2)
        assert first.next is not None
        second = await alice.list(limit=2, after=first.next)

    # Assert
    assert isinstance(first, JobPage)
    assert [info.id for info in first.jobs] == [ids[2], ids[1]]
    assert [info.id for info in second.jobs] == [ids[0]]
    assert second.next is None


async def test_job_list_keeps_only_the_status_asked() -> None:
    """`status` takes a `JobStatus`, or its value as a string."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        view = job.for_owner("alice")
        submitted = await view.submit()
        await _settle(view, submitted.id)
        # Act
        succeeded = await view.list(status=JobStatus.SUCCEEDED)
        by_value = await view.list(status="succeeded")  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]
        running = await view.list(status=JobStatus.RUNNING)

    # Assert
    assert [info.id for info in succeeded.jobs] == [submitted.id]
    assert by_value == succeeded
    assert running == JobPage(jobs=[], next=None)


@pytest.mark.parametrize("limit", [0, 101, -1, True, 1.5, "5", None])
async def test_job_list_refuses_a_limit_out_of_range(
    limit: Any,  # noqa: ANN401
) -> None:
    """`limit` takes an int from 1 to 100, and raises `ValueError` else."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act / Assert
        with pytest.raises(ValueError, match="limit must be an int from 1"):
            await job.for_owner("alice").list(limit=limit)


@pytest.mark.parametrize("limit", [1, 100])
async def test_job_list_takes_a_limit_at_each_bound(limit: int) -> None:
    """`limit` takes 1 and 100."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act
        page = await job.for_owner("alice").list(limit=limit)

    # Assert
    assert page == JobPage(jobs=[], next=None)


@pytest.mark.parametrize("after", ["", "nope", "MA", 5])
async def test_job_list_refuses_a_cursor_no_page_returned(
    after: Any,  # noqa: ANN401
) -> None:
    """An `after` that is no page's `next` raises `ValueError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act / Assert
        with pytest.raises(ValueError, match="cursor"):
            await job.for_owner("alice").list(after=after)


async def test_job_list_refuses_a_status_that_is_not_one() -> None:
    """A `status` that names no status raises `ValueError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        # Act / Assert
        with pytest.raises(ValueError, match="done"):
            await job.for_owner("alice").list(status="done")  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


async def test_job_list_without_an_owner_raises() -> None:
    """Jobs with no owner are never listed, since their id protects them."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        await job.without_owner().submit()
        # Act / Assert
        with pytest.raises(TypeError, match=r"list\(\) needs an owner"):
            await job.without_owner().list()


async def test_job_page_serializes_for_an_endpoint() -> None:
    """A `JobPage` is a frozen Pydantic model with `jobs` and `next`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    async with tasks:
        view = job.for_owner("alice")
        await view.submit()
        submitted = await view.submit()
        page = await view.list(limit=1)

    # Act
    data = json.loads(page.model_dump_json())

    # Assert
    assert list(data) == ["jobs", "next"]
    assert data["jobs"][0]["id"] == submitted.id
    assert isinstance(data["next"], str)
    assert JobPage.model_validate_json(page.model_dump_json()) == page
    with pytest.raises(ValueError, match="frozen"):
        page.next = None  # type: ignore[misc] # ty: ignore[invalid-assignment]


_FREEZE_AFTER_IMPORT = """
import datetime
real_datetime = datetime.datetime
from freezegun import freeze_time
from pydantic import BaseModel

class Loaded(BaseModel):
    count: int

import grelmicro.task

with freeze_time():
    pass

print(datetime.datetime is real_datetime, grelmicro.task.JobInfo.__name__)
"""
"""Load Pydantic, import `grelmicro.task`, then freeze time once."""


def test_freeze_time_after_task_import_leaves_datetime_real() -> None:
    """`freeze_time()` starts and stops cleanly after `import grelmicro.task`."""
    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _FREEZE_AFTER_IMPORT],
        capture_output=True,
        check=False,
        text=True,
    )

    # Assert
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "JobInfo"]
