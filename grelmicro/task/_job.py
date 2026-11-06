"""On-demand jobs.

A job runs once, when code submits it, and reports its progress while it
runs. One `Job` is one `Task` of its `Tasks`, so its runs drain and record
like scheduled fires.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import secrets
import threading
from annotationlib import Format
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    NamedTuple,
    get_args,
    get_origin,
)

from fast_depends.dependencies.model import Dependant  # codespell:ignore
from fast_depends.library import CustomField
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._caller import subject_of
from grelmicro._duration import check_duration, check_retention, is_int
from grelmicro._task import Task
from grelmicro.clock._seam import monotonic, sleep
from grelmicro.errors import OutOfContextError
from grelmicro.task._fire import FireOutcome, FireRecorder, all_of, first_leaf
from grelmicro.task._job_arguments import JobArguments, resolve_annotation
from grelmicro.task._job_loop import BEAT, LEASE, JobStoreLoop, store_loop
from grelmicro.task._job_trace import (
    mark_failed,
    mark_lost,
    name_error,
    record_exception,
    run_span,
    submitter_context,
)
from grelmicro.task._protocol import (
    JOB_LOST,
    Beat,
    BeatReply,
    Claimed,
    EndStatus,
    JobBackend,
    JobRecord,
    JobStatus,
    ProgressUpdate,
)
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.errors import JobError, JobRefusedError
from grelmicro.task.memory import MemoryJobAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from grelmicro.security.principal import Principal

logger = getLogger("grelmicro.task")

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
"""Checks that a returned result is JSON."""

_FAILED = "Job failed"
"""The error a caller sees for any exception but a `JobError`."""


class JobInfo(BaseModel, frozen=True):
    """A read-only snapshot of one job.

    Every read returns a new one. It never carries the job's owner.

    Read more in the [On-demand jobs](../task.md#on-demand-jobs) docs.
    """

    id: Annotated[str, Doc("The job id, URL-safe.")]
    name: Annotated[str, Doc("The name of the job's function.")]
    status: Annotated[JobStatus, Doc("Where the job is in its run.")]
    done: Annotated[int, Doc("Items the job reported done.")]
    total: Annotated[
        int | None, Doc("Items the job expects, or `None` until it says.")
    ]
    result: Annotated[
        JsonValue | None,
        Doc("What the body returned, or `None` until it does."),
    ]
    error: Annotated[
        str | None, Doc("Why the job failed, or `None` unless it failed.")
    ]
    cancel_requested: Annotated[bool, Doc("Whether a cancel was requested.")]
    created_at: Annotated[datetime, Doc("When the job was submitted.")]
    started_at: Annotated[
        datetime | None, Doc("When the body started, or `None` before.")
    ]
    finished_at: Annotated[
        datetime | None,
        Doc("When the job reached an end state, or `None` before."),
    ]


class _Outcome(NamedTuple):
    """How a job ended, as this replica writes it to the store."""

    status: EndStatus
    error: str | None
    result: str | None
    progress: ProgressUpdate


class _Run:
    """A job this replica is running: its unwritten counts and its task."""

    __slots__ = (
        "cancel_requested",
        "done",
        "id",
        "lock",
        "outcome",
        "sending",
        "started",
        "task",
        "total",
        "written",
    )

    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.lock = threading.Lock()
        self.done = 0
        self.total: int | None = None
        self.written = ProgressUpdate(done=0, total=None)
        self.sending: ProgressUpdate | None = None
        self.cancel_requested = False
        self.started = False
        self.outcome: _Outcome | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def ended(self) -> bool:
        """Whether the job ended, though the store may not have it yet."""
        return self.outcome is not None

    def unwritten_counts(self) -> ProgressUpdate | None:
        """Return the counts when the store does not have them yet."""
        with self.lock:
            counts = ProgressUpdate(done=self.done, total=self.total)
        self.sending = None if counts == self.written else counts
        return self.sending

    def counts_written(self) -> None:
        """Note that the store now has the counts the last beat sent."""
        if self.sending is not None:
            self.written = self.sending
            self.sending = None

    def end(
        self, status: EndStatus, error: str | None, result: str | None
    ) -> None:
        """Refuse any later report, and keep how the job ended."""
        with self.lock:
            self.outcome = _Outcome(
                status,
                error,
                result,
                ProgressUpdate(done=self.done, total=self.total),
            )


class Progress:
    """How a job body reports its progress.

    Take it as the body's first parameter. The job passes it in.

    Read more in the [Progress](../task.md#progress) docs.
    """

    __slots__ = ("_job", "_run")

    def __init__(self, run: _Run, job: Job[...]) -> None:
        self._run = run
        self._job = job

    @property
    def total(self) -> int | None:
        """Items the job expects, or `None` until set."""
        return self._run.total

    @total.setter
    def total(self, value: int | None) -> None:
        if value is not None:
            _check_count(value, "total")
        with self._run.lock:
            self._check_live()
            self._run.total = value

    @property
    def done(self) -> int:
        """Items reported done so far."""
        return self._run.done

    @property
    def stopping(self) -> bool:
        """Whether the job should stop.

        `True` once a cancel was requested, or once its tasks started
        shutting down.
        """
        return self._run.cancel_requested or self._job._stopping  # noqa: SLF001

    def _check_live(self) -> None:
        """Stop a body that reports after its job ended.

        Raises:
            asyncio.CancelledError: If the job already ended, such as a
                sync job cancelled at the shutdown timeout.
        """
        if self._run.ended:
            raise asyncio.CancelledError

    def checkpoint(self) -> None:
        """Stop here when the job should stop.

        Raises `asyncio.CancelledError` once `stopping` is `True`, so the
        job ends `cancelled` with the counts reported so far. Call it
        between two items, where stopping is safe. A sync body stops only
        at a checkpoint.
        """
        if self.stopping:
            raise asyncio.CancelledError

    def advance(
        self,
        n: Annotated[int, Doc("Items done since the last call.")] = 1,
    ) -> None:
        """Report `n` more items done.

        Raises:
            TypeError: If `n` is not an `int`.
            ValueError: If `n` is negative.
            asyncio.CancelledError: If the job already ended.
        """
        _check_count(n, "n")
        with self._run.lock:
            self._check_live()
            self._run.done += n


class Job[**P](Task):
    """A function that runs once each time it is submitted.

    Use the `Tasks.job()` or `TaskRouter.job()` decorator instead of
    creating a `Job` directly.

    Read more in the [On-demand jobs](../task.md#on-demand-jobs) docs.
    """

    _starts_first = True
    """`Tasks` starts every job before its other tasks."""

    def __init__(
        self,
        function: Callable[..., object],
        *,
        name: str | None = None,
        max_concurrent: int = 1,
        max_queued: int | None = None,
        max_wait: int | timedelta | None = 86400,
        keep: int | timedelta | None = 86400,
    ) -> None:
        """Initialize the job.

        Raises:
            FunctionTypeError: If the function is not supported.
            TypeError: If the function does not take `progress` first.
            ValueError: If `max_concurrent` is not an int of at least 1,
                `max_queued` is not an int of at least 0, `max_wait` is not
                whole seconds or a `timedelta` above zero, `keep` is not whole
                seconds or a `timedelta` from zero, or either is over 100
                years.
        """
        alt_name = validate_and_generate_reference(function)
        signature = _check_progress_first(function)
        if not is_int(max_concurrent) or max_concurrent < 1:
            msg = f"max_concurrent must be an int of at least 1, got {max_concurrent!r}"
            raise ValueError(msg)
        if max_queued is not None and (
            not is_int(max_queued) or max_queued < 0
        ):
            msg = f"max_queued must be an int of at least 0, got {max_queued!r}"
            raise ValueError(msg)
        self._name = name or alt_name
        self._arguments = JobArguments(self._name, function, signature)
        self._function = function
        self._interruptible = is_async_callable(function)
        self._clock = partial(datetime.now, UTC)
        self._fire = FireRecorder(
            self._name,
            function,
            clock=self._clock,
            cast=False,
            expected=(JobError,),
            finish=_json_result,
            count_later=True,
        )
        self._max_concurrent = max_concurrent
        self._max_queued = max_queued
        self._max_wait = (
            None if max_wait is None else check_duration(max_wait, "max_wait")
        )
        self._keep = None if keep is None else check_retention(keep, "keep")
        self._backend: JobBackend = MemoryJobAdapter()
        self._loop: JobStoreLoop | None = None
        self._runs: dict[str, _Run] = {}
        self._running = 0
        self._reserved = 0
        self._submitting = 0
        self._no_submits = asyncio.Event()
        self._no_submits.set()
        self._group: asyncio.TaskGroup | None = None
        self._context: contextvars.Context | None = None
        self._stopping = False

    @property
    def name(self) -> str:
        """The job name."""
        return self._name

    def __repr__(self) -> str:
        """Return the job as `Job('<name>')`."""
        return f"Job({self._name!r})"

    @property
    def function(self) -> Callable[..., Any]:
        """The function the job runs, as it was registered."""
        return self._function

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run submitted jobs until `stop` is set, then drain them.

        On `stop`, this replica refuses new submissions and claims nothing
        more, queued jobs stay queued in the store, and running jobs see
        `Progress.stopping`. A submit the store has not answered yet still
        runs the job it starts. The call returns once every running job has
        ended. When `Tasks` cancels it at its `shutdown_timeout`, the jobs
        still running are cancelled, and a submit answered later is refused.
        """
        stopped = stop or asyncio.Event()
        self._stopping = False
        self._context = contextvars.copy_context()
        loop = store_loop(self._backend)
        await loop.join(self)
        self._loop = loop
        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                if ready is not None and not ready.done():  # pragma: no branch
                    ready.set_result(None)
                try:
                    await stopped.wait()
                finally:
                    self._stopping = True
                    await loop.finish_round()
                    if not _cancelling():
                        await self._no_submits.wait()
        finally:
            self._group = None
            try:
                await self._end_unsettled()
            finally:
                await loop.leave(self)

    async def _end_unsettled(self) -> None:
        """Write the end of each job whose task ended before the store took it.

        A task cancelled before its first step ends `cancelled`.
        """
        for run in list(self._runs.values()):
            if run.outcome is None:
                run.end(JobStatus.CANCELLED, None, None)
            await self._end_in_store(run, retry=False)

    def _beats(self) -> list[Beat]:
        """Return one entry per running job, with counts the store lacks."""
        return [
            Beat(self._name, run.id, run.unwritten_counts())
            for run in self._runs.values()
        ]

    def _read_reply(self, reply: BeatReply) -> None:
        """Stop a run that was cancelled, or that this replica lost."""
        run = self._runs.get(reply.job_id)
        if run is None:
            return
        if reply.held:
            run.counts_written()
        if not reply.held or reply.cancel_requested:
            self._ask_to_stop(run)

    def _stop_runs(self) -> None:
        """Ask every job this replica runs to stop."""
        for run in self._runs.values():
            self._ask_to_stop(run)

    def _ask_to_stop(self, run: _Run) -> None:
        """Flag `run`, and cancel its task when its body can be interrupted.

        A run that already ended is left to write its end.
        """
        if run.cancel_requested:
            return
        run.cancel_requested = True
        if (
            self._interruptible
            and run.started
            and not run.ended
            and run.task is not None
        ):
            run.task.cancel()

    def _reserve_slots(self) -> int:
        """Reserve and return every free slot, none when stopping."""
        if self._stopping or self._group is None:
            return 0
        free = self._max_concurrent - self._running
        self._running += free
        self._reserved = free
        return free

    def _give_back_slots(self, slots: int) -> None:
        """Give back slots a claim did not use."""
        self._running -= slots
        self._reserved = 0

    def _accept(self, job: Claimed) -> None:
        """Start a job the loop claimed, in a slot it reserved."""
        assert self._group is not None  # noqa: S101
        self._start(self._group, job)

    def _wake_loop(self) -> None:
        """Look for queued jobs now that a slot is free."""
        assert self._loop is not None  # noqa: S101
        self._loop.wake()

    def _start(self, group: asyncio.TaskGroup, job: Claimed) -> None:
        """Start the body of a job this replica holds, in a slot it reserved."""
        run = _Run(job.record.id)
        self._runs[run.id] = run
        assert self._context is not None  # noqa: S101
        run.task = group.create_task(
            self._run_in_span(run, job), context=self._context.copy()
        )
        run.task.add_done_callback(self._release)

    def _release(self, _task: asyncio.Task[None]) -> None:
        """Free the slot of a job task that ended, started or not."""
        self._running -= 1
        self._wake_loop()

    def for_owner(
        self,
        owner: Annotated[
            str | Principal,
            Doc(
                """
                Who the jobs belong to: the authenticated caller, or a
                non-empty string your service assigns, such as a user id.
                """
            ),
        ],
    ) -> JobView[P]:
        """Return this job as `owner` sees it.

        `submit` on it records `owner` with the job, and `get` and `cancel`
        find only the jobs `owner` submitted. A caller is keyed by its issuer
        and subject together.

        Raises:
            TypeError: If `owner` is neither a string nor a caller.
            ValueError: If `owner` is empty, or a caller that was not
                authenticated or has no subject.
        """
        return JobView(self, _owner_key(owner))

    def without_owner(self) -> JobView[P]:
        """Return this job for the jobs that have no owner.

        Only its id protects such a job, so use this for a job your service
        submits for itself, whose id never reaches a caller. Use `for_owner`
        for any job a caller reaches.
        """
        return JobView(self, None)

    async def _cancel(self, job_id: str, owner: str | None) -> JobInfo | None:
        """Ask the job `job_id` to stop when `owner` owns it."""
        record = await self._backend.cancel(
            name=self._name, job_id=job_id, owner=owner
        )
        if record is None:
            return None
        run = self._runs.get(job_id)
        if run is not None:
            self._ask_to_stop(run)
        return _info(record)

    async def _submit(
        self,
        owner: str | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> JobInfo:
        """Store a job for `owner`, and start it when a slot is free."""
        payload = self._arguments.encode(args, kwargs)
        context = submitter_context()
        while (
            self._loop is not None
            and not self._stopping
            and self._reserved
            and self._running >= self._max_concurrent
        ):
            await self._loop.finish_round()
        if self._stopping:
            raise JobRefusedError(
                name=self._name, reason="the tasks are shutting down"
            )
        group, loop = self._group, self._loop
        if group is None or loop is None:
            raise OutOfContextError(self, "submit")
        free = self._running < self._max_concurrent
        if free:
            self._running += 1
        started = False
        self._submitting += 1
        self._no_submits.clear()
        try:
            submitted = await self._backend.submit(
                name=self._name,
                job_id=secrets.token_urlsafe(16),
                owner=owner,
                payload=payload,
                context=context,
                max_queued=self._max_queued,
                max_wait=self._max_wait,
                keep=self._keep,
                start_as=loop.runner if free else None,
                lease=LEASE,
            )
            started = submitted.started
        finally:
            self._submitting -= 1
            if not self._submitting:
                self._no_submits.set()
            if free and not started:
                self._running -= 1
                self._wake_loop()
        if submitted.record is None:
            raise JobRefusedError(name=self._name, reason="the queue is full")
        if started:
            if self._group is not group:
                self._running -= 1
                raise JobRefusedError(
                    name=self._name, reason="the tasks are shutting down"
                )
            self._start(group, Claimed(submitted.record, payload, context))
        return _info(submitted.record)

    async def _get(self, job_id: str, owner: str | None) -> JobInfo | None:
        """Return the job `job_id` when `owner` owns it, else `None`."""
        record = await self._backend.get(
            name=self._name, job_id=job_id, owner=owner
        )
        return None if record is None else _info(record)

    async def _settle(
        self,
        run: _Run,
        status: EndStatus,
        error: str | None = None,
        result: str | None = None,
    ) -> bool:
        """End a job this replica holds, and return whether the store took it.

        A job the store no longer gives this replica stays as the store has
        it. That is logged, without the job's id, and marked on the span.
        """
        run.end(status, error, result)
        if status is JobStatus.FAILED:
            mark_failed(error or _FAILED)
        settled = await self._end_in_store(run, retry=True)
        if not settled:
            mark_lost(JOB_LOST)
        return settled

    async def _end_in_store(self, run: _Run, *, retry: bool) -> bool:
        """Write how `run` ended, stop tracking it, and return whether it took.

        With `retry`, a store error is tried again each beat for one lease,
        while the beats keep the job held. A run cancelled meanwhile stays
        tracked, so the job's stop writes its end once more.
        """
        assert self._loop is not None  # noqa: S101
        assert run.outcome is not None  # noqa: S101
        give_up_at = monotonic() + LEASE.total_seconds()
        while True:
            try:
                settled = await self._backend.settle(
                    name=self._name,
                    job_id=run.id,
                    runner=self._loop.runner,
                    status=run.outcome.status,
                    error=run.outcome.error,
                    result=run.outcome.result,
                    progress=run.outcome.progress,
                )
            except Exception:
                if retry and monotonic() < give_up_at:
                    logger.debug(
                        "Job end failed, retrying: %s",
                        self._name,
                        exc_info=True,
                    )
                    await sleep(BEAT.total_seconds())
                    continue
                logger.warning(
                    "Job could not be ended in its store: %s",
                    self._name,
                    exc_info=True,
                )
                settled = False
            break
        self._runs.pop(run.id, None)
        if not settled:
            logger.warning(
                "Job lost, its lease ran out before it ended: %s", self._name
            )
        return settled

    async def _run_in_span(self, run: _Run, job: Claimed) -> None:
        """Run one job inside its span, linked to the code that submitted it."""
        with run_span(self._name, run.id, job.context):
            await self._run(run, job.record, job.payload)

    async def _run(self, run: _Run, record: JobRecord, payload: str) -> None:
        """Run one job's body and settle it.

        A job asked to stop before its first step ends `cancelled` and never
        calls the body. Arguments that cannot be loaded, whatever loading
        raises, end the job `failed`, and so does a body that returns anything
        but JSON. A body that returns while its task is being
        cancelled ends `cancelled`. One that raises a `BaseException` other
        than a cancel ends `failed`, the exception propagates, and this
        replica claims nothing more. The run is counted once the store has
        its end, so a job the store lost never counts as a success.
        """
        assert record.started_at is not None  # noqa: S101
        try:
            if run.cancel_requested:
                await self._settle(run, JobStatus.CANCELLED)
                return
            try:
                args, kwargs = self._arguments.decode(payload)
            except Exception as error:  # noqa: BLE001
                name_error(error)
                self._fire.unrun(record.started_at, FireOutcome.ERROR, error)
                logger.error(  # noqa: TRY400
                    "Job arguments could not be loaded: %s: %s",
                    self._name,
                    _load_error_types(error),
                )
                await self._settle(run, JobStatus.FAILED, _FAILED)
                return
            run.started = True
            ran = await self._fire.run(
                record.started_at, None, Progress(run, self), *args, **kwargs
            )
            if _cancelling():
                self._fire.count(record.started_at, ran, run=False)
                await self._settle(run, JobStatus.CANCELLED)
            elif ran.error is not None:
                name_error(ran.error)
                if not all_of(ran.error, (JobError,)):
                    record_exception(ran.error)
                self._fire.count(record.started_at, ran)
                await self._settle(
                    run, JobStatus.FAILED, _caller_error(ran.error)
                )
            else:
                settled = False
                try:
                    settled = await self._settle(
                        run, JobStatus.SUCCEEDED, result=ran.value
                    )
                finally:
                    self._fire.count(
                        record.started_at,
                        ran,
                        error_type=None if settled else "job_lost",
                    )
        except asyncio.CancelledError:
            if not run.ended:
                await self._settle(run, JobStatus.CANCELLED)
            raise
        except BaseException as error:
            self._stopping = True
            name_error(error)
            if not run.ended:
                await self._settle(run, JobStatus.FAILED, _FAILED)
            raise


def _owner_key(owner: str | Principal) -> str:
    """Return what the store records for `owner`, refusing a bad one."""
    if isinstance(owner, str):
        if not owner:
            msg = "for_owner() needs a non-empty owner."
            raise ValueError(msg)
        return owner
    if not all(
        hasattr(owner, name)
        for name in ("is_authenticated", "subject", "issuer")
    ):
        msg = (
            "for_owner() takes a string or an authenticated caller, "
            f"got {type(owner).__name__}."
        )
        raise TypeError(msg)
    subject = subject_of(owner)
    if subject is None:
        msg = "for_owner() needs an authenticated caller with a subject."
        raise ValueError(msg)
    return json.dumps([owner.issuer, subject])


class JobView[**P]:
    """A `Job` for one owner, or for the jobs with no owner.

    Use `Job.for_owner()` or `Job.without_owner()` instead of creating one
    directly.

    Read more in the [Owner](../task.md#owner) docs.
    """

    __slots__ = ("_job", "_owner")

    def __init__(self, job: Job[P], owner: str | None) -> None:
        self._job = job
        self._owner = owner

    async def submit(self, /, *args: P.args, **kwargs: P.kwargs) -> JobInfo:
        """Submit a job with the function's arguments, after `progress`.

        Returns at once, before the body starts.

        Raises:
            TypeError: If the function cannot take these arguments.
            OutOfContextError: If the tasks never started.
            JobRefusedError: If the queue is full, or the tasks are
                shutting down.
        """
        return await self._job._submit(self._owner, args, kwargs)  # noqa: SLF001

    async def get(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Return the job `job_id`.

        Returns `None` when no job has that id, or when it belongs to
        another owner, so a caller cannot tell them apart.
        """
        return await self._job._get(job_id, self._owner)  # noqa: SLF001

    async def cancel(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Ask the job `job_id` to stop, and return it at once.

        A queued job ends `cancelled` right away. A running async body gets
        `asyncio.CancelledError` at its next `await`, within about a second
        on another replica. Cancelling a finished job changes nothing.

        Returns `None` when no job has that id, or when it belongs to
        another owner. Such a job is left as it is.
        """
        return await self._job._cancel(job_id, self._owner)  # noqa: SLF001


def _check_progress_first(function: Callable[..., Any]) -> inspect.Signature:
    """Return the arguments `submit` takes, refusing a body without `progress`.

    The first parameter must be a positional `Progress`, or unannotated
    and named `progress`. Parameters the body has injected, written as
    `client: Client = Depends(get_client)` or
    `client: Annotated[Client, Depends(get_client)]`, are left out of the
    returned signature. Only the first annotation must resolve now.

    Raises:
        TypeError: If the first parameter is missing, keyword-only, or
            annotated with another type.
        NameError: If the first annotation names something the runtime
            cannot resolve.
    """
    signature = inspect.signature(function, annotation_format=Format.FORWARDREF)
    parameters = list(signature.parameters.values())
    first = parameters[0] if parameters else None
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    if (
        first is None
        or first.kind not in positional
        or not _is_progress(first, function)
    ):
        msg = (
            f"A job takes `progress: Progress` as its first parameter, "
            f"{getattr(function, '__qualname__', function)!r} does not"
        )
        raise TypeError(msg)
    return signature.replace(
        parameters=[p for p in parameters if not _is_injected(p, function)]
    )


def _is_progress(
    parameter: inspect.Parameter, function: Callable[..., Any]
) -> bool:
    """Return whether `parameter` takes the job's `Progress`."""
    annotation = resolve_annotation(parameter.annotation, function)
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if annotation is inspect.Parameter.empty:
        return parameter.name == "progress"
    return annotation is Progress


def _is_injected(
    parameter: inspect.Parameter, function: Callable[..., Any]
) -> bool:
    """Return whether dependency injection fills `parameter`.

    An annotation the runtime cannot resolve yet counts as not injected.
    """
    markers = Dependant | CustomField  # codespell:ignore
    if isinstance(parameter.default, markers):
        return True
    try:
        annotation = resolve_annotation(parameter.annotation, function)
    except NameError:
        return False
    return get_origin(annotation) is Annotated and any(
        isinstance(marker, markers) for marker in get_args(annotation)[1:]
    )


def _json_result(value: object) -> str:
    """Return what a body returned as JSON text.

    Raises:
        TypeError: If `value` is not JSON.
    """
    try:
        return json.dumps(_JSON.validate_python(value), allow_nan=False)
    except ValueError:
        msg = f"A job body must return JSON, got a {type(value).__name__}"
        raise TypeError(msg) from None


def _caller_error(error: Exception) -> str:
    """Return the error a caller sees for `error`.

    A `JobError`, or a group made only of them, shows its first message.
    Anything else shows as `"Job failed"`.
    """
    if not all_of(error, (JobError,)):
        return _FAILED
    shown = first_leaf(error) if isinstance(error, ExceptionGroup) else error
    return str(shown) or _FAILED


def _load_error_types(error: Exception) -> list[str]:
    """Return what kind of errors loading raised, never their values."""
    if isinstance(error, ValidationError):
        return [detail["type"] for detail in error.errors()]
    return [type(error).__name__]


def _cancelling() -> bool:
    """Return whether the current task is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _check_count(value: object, what: str) -> None:
    """Refuse a count that is not a non-negative `int`.

    Raises:
        TypeError: If `value` is not an `int`, or is a `bool`.
        ValueError: If `value` is negative.
    """
    if not is_int(value):
        msg = f"{what} must be an int, got {type(value).__name__}"
        raise TypeError(msg)
    if value < 0:
        msg = f"{what} must not be negative, got {value}"
        raise ValueError(msg)


def _info(record: JobRecord) -> JobInfo:
    """Return what a caller sees of a stored job."""
    return JobInfo(
        id=record.id,
        name=record.name,
        status=record.status,
        done=record.done,
        total=record.total,
        result=None if record.result is None else json.loads(record.result),
        error=record.error,
        cancel_requested=record.cancel_requested,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
