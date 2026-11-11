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
import time
from annotationlib import Format
from contextlib import suppress
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
from grelmicro._current_app import _current_micro
from grelmicro._duration import check_duration, check_retention, is_int
from grelmicro._environment import (
    Binding,
    falls_short,
    label,
    record,
    required_scope,
)
from grelmicro._task import Task
from grelmicro.clock._seam import monotonic, sleep
from grelmicro.errors import OutOfContextError
from grelmicro.task._fire import (
    FireOutcome,
    FireRecorder,
    FireResult,
    all_of,
    first_leaf,
)
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
    JOB_TIMED_OUT,
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
from grelmicro.types import BackendScope

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
        "ended_by",
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
        self.ended_by: str | None = None
        """The error the store ended the job with, once a beat or settle said."""
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

    def lost(self) -> tuple[str, str]:
        """Return why the store took the job from this replica.

        The first item is the job's error, the second its `error.type`. A
        job the store took with no reason given reads as lost.
        """
        if self.ended_by == JOB_TIMED_OUT:
            return JOB_TIMED_OUT, "job_timed_out"
        return JOB_LOST, "job_lost"

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
                sync job cancelled at the shutdown timeout, or one the store
                ended past its `max_run`.
        """
        if self._run.ended or self._run.ended_by is not None:
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
        max_run: int | timedelta | None = None,
        keep: int | timedelta | None = 86400,
        backend: JobBackend | None = None,
        requires: BackendScope | None = None,
    ) -> None:
        """Initialize the job.

        Raises:
            FunctionTypeError: If the function is not supported.
            TypeError: If the function does not take `progress` first.
            ValueError: If `max_concurrent` is not an int of at least 1,
                `max_queued` is not an int of at least 0, `max_wait` or
                `max_run` is not whole seconds or a `timedelta` above zero,
                `keep` is not whole seconds or a `timedelta` from zero, or
                any of them is over 100 years.
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
        self._max_run = (
            None if max_run is None else check_duration(max_run, "max_run")
        )
        self._keep = None if keep is None else check_retention(keep, "keep")
        self._requires = required_scope(requires, "cluster")
        self._chosen = backend
        self._fallback = MemoryJobAdapter()
        self._backend: JobBackend = (
            self._fallback if backend is None else backend
        )
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
        binding = self._scope_binding()
        if binding.backend is None or falls_short(
            binding.backend, self._requires
        ):
            record(self, binding)

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

    @property
    def requires(self) -> BackendScope:
        """The smallest scope the job's store must reach."""
        return self._requires

    def _scope_binding(self) -> Binding:
        """Describe the store this job runs on, for the scope check.

        A store passed as `backend=` is checked on its own. Any other job
        rides the app's `Coordination`, and without a job store there it
        lives in the process.
        """
        if self._chosen is not None:
            return Binding(
                label(self), self._requires, backend=self._chosen, kind="job"
            )
        return Binding(
            label(self),
            self._requires,
            rides=("coordination", "default"),
            attribute="_job_backend",
            absent=self._fallback,
            kind="job",
        )

    def _resolve_backend(self) -> JobBackend:
        """Return the store this job runs on, read once as its tasks start.

        `backend=` wins. Inside an app, the job takes the store of its
        `Coordination`. Without one, the job keeps its jobs in this process.
        A running job keeps its store, so `micro.override(...)` entered after
        the app opened does not reach it.
        """
        if self._chosen is not None:
            return self._chosen
        micro = _current_micro.get(None)
        if micro is not None:
            from grelmicro._app import resolve_ambient  # noqa: PLC0415

            with suppress(LookupError):
                coordination = resolve_ambient(("coordination", "default"))
                store = getattr(coordination, "_job_backend", None)
                if store is not None:
                    return store
        return self._fallback

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
        self._backend = self._resolve_backend()
        loop = store_loop(self._backend, opens=self._backend is self._fallback)
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
        else:
            run.ended_by = reply.error or JOB_LOST
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
                max_run=self._max_run,
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
            mark_lost(*run.lost())
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
                reply = await self._backend.settle(
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
            else:
                settled = reply.settled
                if not settled and run.ended_by is None:
                    run.ended_by = reply.error
            break
        self._runs.pop(run.id, None)
        if not settled and run.ended_by == JOB_TIMED_OUT:
            logger.warning(
                "Job not finished in time, its max_run passed: %s", self._name
            )
        elif not settled:
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
        its end. A job the store ended itself, past its lease or `max_run`,
        counts as an error of that kind, whatever the body did.
        """
        assert record.started_at is not None  # noqa: S101
        # Set when the body raises a `BaseException`. Every other await here
        # is a settle, which ends the run before it can be interrupted.
        stopped: FireResult | None = None
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
            began = time.perf_counter()
            try:
                ran = await self._fire.run(
                    record.started_at,
                    None,
                    Progress(run, self),
                    *args,
                    **kwargs,
                )
            except BaseException:
                stopped = FireResult(duration=time.perf_counter() - began)
                raise
            await self._end_ran(run, record.started_at, ran)
        except asyncio.CancelledError:
            if not run.ended and stopped is not None:
                await self._end_and_count(
                    run, record.started_at, stopped, JobStatus.CANCELLED
                )
            raise
        except BaseException as error:
            self._stopping = True
            name_error(error)
            if not run.ended and stopped is not None:
                await self._end_and_count(
                    run, record.started_at, stopped, JobStatus.FAILED, _FAILED
                )
            raise

    async def _end_ran(
        self, run: _Run, started_at: datetime, ran: FireResult
    ) -> None:
        """End a job whose body returned or raised an `Exception`."""
        if _cancelling():
            await self._end_and_count(run, started_at, ran, JobStatus.CANCELLED)
        elif ran.error is not None:
            name_error(ran.error)
            if not all_of(ran.error, (JobError,)):
                record_exception(ran.error)
            await self._end_and_count(
                run,
                started_at,
                ran,
                JobStatus.FAILED,
                _caller_error(ran.error),
            )
        else:
            await self._end_and_count(
                run,
                started_at,
                ran,
                JobStatus.SUCCEEDED,
                result=ran.value,
            )

    async def _end_and_count(
        self,
        run: _Run,
        started_at: datetime,
        ran: FireResult,
        status: EndStatus,
        error: str | None = None,
        result: str | None = None,
    ) -> None:
        """End a job whose body ran, then count the run by the store's answer.

        A run the store refused counts as an error of why the store ended
        it. A run the store took counts as the body ended: a success, the
        body's error, or no run for a cancel or a `BaseException`.
        """
        settled = False
        try:
            settled = await self._settle(run, status, error, result)
        finally:
            if not settled:
                self._fire.count(started_at, ran, error_type=run.lost()[1])
            elif status is JobStatus.CANCELLED or (
                status is JobStatus.FAILED and ran.error is None
            ):
                self._fire.count(started_at, ran, run=False)
            else:
                self._fire.count(started_at, ran)


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
