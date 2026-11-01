"""On-demand jobs.

A job runs once, when code submits it, and reports its progress while it
runs. One `Job` is one `Task` of its `Tasks`, so its runs drain and record
like scheduled fires.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import secrets
import threading
from annotationlib import Format, ForwardRef
from collections import deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from logging import getLogger
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin

from fast_depends.dependencies.model import Dependant  # codespell:ignore
from fast_depends.library import CustomField
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._duration import check_retention, is_int
from grelmicro._task import Task
from grelmicro.clock._seam import monotonic, sleep
from grelmicro.errors import OutOfContextError
from grelmicro.task._fire import FireRecorder
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.errors import JobError, JobRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = getLogger("grelmicro.task")

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
"""Checks that a reported result is JSON."""

_FAILED = "Job failed"
"""The error a caller sees for any exception but a `JobError`."""


class JobStatus(StrEnum):
    """Status of a job.

    - ``QUEUED``: waiting for a free slot.
    - ``RUNNING``: the body is running.
    - ``SUCCEEDED``: the body returned.
    - ``FAILED``: the body raised. `JobInfo.error` says why.
    - ``CANCELLED``: cancelled before or while it ran.

    Read more in the [Status](../task.md#status) docs.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    results: Annotated[
        tuple[JsonValue, ...], Doc("Results the job reported, in order.")
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


class _Record:
    """The live state of one job."""

    __slots__ = (
        "args",
        "cancel_requested",
        "context",
        "created_at",
        "done",
        "error",
        "expires_at",
        "finished_at",
        "id",
        "kwargs",
        "lock",
        "owner",
        "results",
        "started_at",
        "status",
        "task",
        "total",
    )

    def __init__(
        self,
        job_id: str,
        owner: str | None,
        created_at: datetime,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self.id = job_id
        self.owner = owner
        self.args = args
        self.kwargs = kwargs
        self.context: contextvars.Context | None = contextvars.copy_context()
        self.lock = threading.Lock()
        self.status = JobStatus.QUEUED
        self.done = 0
        self.total: int | None = None
        self.results: list[JsonValue] = []
        self.error: str | None = None
        self.created_at = created_at
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.expires_at = 0.0
        self.cancel_requested = False
        self.task: asyncio.Task[None] | None = None


class Progress:
    """How a job body reports its progress.

    Take it as the body's first parameter. The job passes it in.

    Read more in the [Progress](../task.md#progress) docs.
    """

    __slots__ = ("_job", "_record")

    def __init__(self, record: _Record, job: Job[...]) -> None:
        self._record = record
        self._job = job

    @property
    def total(self) -> int | None:
        """Items the job expects, or `None` until set."""
        return self._record.total

    @total.setter
    def total(self, value: int | None) -> None:
        if value is not None:
            _check_count(value, "total")
        with self._record.lock:
            self._check_live()
            self._record.total = value

    @property
    def done(self) -> int:
        """Items reported done so far."""
        return self._record.done

    @property
    def stopping(self) -> bool:
        """Whether the job should stop.

        `True` once a cancel was requested, or once its tasks started
        shutting down.
        """
        return self._record.cancel_requested or self._job._stopping  # noqa: SLF001

    def _check_live(self) -> None:
        """Stop a body that reports after its job ended.

        Raises:
            asyncio.CancelledError: If the job already ended, such as a
                sync job cancelled at the shutdown timeout.
        """
        if self._record.finished_at is not None:
            raise asyncio.CancelledError

    def checkpoint(self) -> None:
        """Stop here when the job should stop.

        Raises `asyncio.CancelledError` once `stopping` is `True`, so the
        job ends `cancelled` with the results reported so far. Call it
        between two items, where stopping is safe. A sync body stops only
        at a checkpoint.
        """
        if self.stopping:
            raise asyncio.CancelledError

    def advance(
        self,
        n: Annotated[int, Doc("Items done since the last call.")] = 1,
        *,
        result: Annotated[
            JsonValue | None,
            Doc("A result to add to `results`. `None` adds nothing."),
        ] = None,
    ) -> None:
        """Report `n` more items done, with an optional result.

        The job keeps a copy of `result`, so changing it afterwards does
        not change `results`.

        Raises:
            TypeError: If `n` is not an `int`, or `result` is not JSON: a
                string, a number, a boolean, or a list or dict of them.
            ValueError: If `n` is negative.
            asyncio.CancelledError: If the job already ended.
        """
        _check_count(n, "n")
        if result is not None:
            try:
                result = _JSON.validate_python(result, strict=True)
            except ValidationError:
                msg = (
                    "A job result must be JSON: a string, a number, a "
                    "boolean, or a list or dict of them"
                )
                raise TypeError(msg) from None
        record = self._record
        with record.lock:
            self._check_live()
            record.done += n
            if result is not None:
                record.results.append(result)


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
        function: Callable[..., Awaitable[None] | None],
        *,
        name: str | None = None,
        max_concurrent: int = 1,
        max_queued: int | None = 100,
        keep: int | timedelta | None = 3600,
    ) -> None:
        """Initialize the job.

        Raises:
            FunctionTypeError: If the function is not supported.
            TypeError: If the function does not take `progress` first.
            ValueError: If `max_concurrent` is not an int of at least 1,
                `max_queued` is not an int of at least 0, or `keep` is
                not whole seconds or a `timedelta`, is negative, or is
                over 100 years.
        """
        alt_name = validate_and_generate_reference(function)
        self._signature = _check_progress_first(function)
        if not is_int(max_concurrent) or max_concurrent < 1:
            msg = f"max_concurrent must be an int of at least 1, got {max_concurrent!r}"
            raise ValueError(msg)
        if max_queued is not None and (
            not is_int(max_queued) or max_queued < 0
        ):
            msg = f"max_queued must be an int of at least 0, got {max_queued!r}"
            raise ValueError(msg)
        self._name = name or alt_name
        self._function = function
        self._interruptible = is_async_callable(function)
        self._clock = partial(datetime.now, UTC)
        self._fire = FireRecorder(
            self._name,
            function,
            clock=self._clock,
            cast=False,
            expected=(JobError,),
        )
        self._max_concurrent = max_concurrent
        self._max_queued = max_queued
        self._keep = (
            None
            if keep is None
            else check_retention(keep, "keep").total_seconds()
        )
        self._finished: deque[_Record] = deque()
        self._kept = asyncio.Event()
        self._records: dict[str, _Record] = {}
        self._unfinished: set[_Record] = set()
        self._queue: deque[_Record] = deque()
        self._running = 0
        self._group: asyncio.TaskGroup | None = None
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

        On `stop`, new submissions are refused, queued jobs end
        `cancelled`, and running jobs see `Progress.stopping`. The call
        returns once every running job has ended. When `Tasks` cancels it
        at its `shutdown_timeout`, the jobs still running are cancelled.
        """
        stopped = stop or asyncio.Event()
        self._stopping = False
        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                if ready is not None and not ready.done():  # pragma: no branch
                    ready.set_result(None)
                sweeper = group.create_task(self._sweep())
                try:
                    await stopped.wait()
                finally:
                    sweeper.cancel()
                    self._stop_accepting()
        finally:
            self._group = None

    def _stop_accepting(self) -> None:
        """Refuse new submissions and cancel every job not started yet."""
        self._stopping = True
        self._queue.clear()
        for record in [
            r for r in self._unfinished if r.status is JobStatus.QUEUED
        ]:
            self._settle(record, JobStatus.CANCELLED)

    def for_owner(
        self,
        owner: Annotated[
            str, Doc("Who the jobs belong to, such as a user id.")
        ],
    ) -> OwnedJob[P]:
        """Return this job as `owner` sees it.

        `submit` on it records `owner` with the job, and `get` finds only
        the jobs `owner` submitted. The job itself, without `for_owner`,
        finds only the jobs submitted without an owner.
        """
        return OwnedJob(self, owner)

    async def submit(self, *args: P.args, **kwargs: P.kwargs) -> JobInfo:
        """Submit a job with the function's arguments, after `progress`.

        Returns at once, before the body starts. The job has no owner, so
        only this job, without `for_owner`, finds it.

        Raises:
            TypeError: If the function cannot take these arguments.
            OutOfContextError: If the tasks never started.
            JobRefusedError: If the queue is full, or the tasks are
                shutting down.
        """
        return self._submit(None, args, kwargs)

    async def get(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Return the job `job_id`.

        Returns `None` when no job has that id, or when it has an owner,
        so a caller cannot tell the two apart.
        """
        return self._get(job_id, None)

    async def cancel(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Ask the job `job_id` to stop, and return it at once.

        A queued job ends `cancelled` right away. A running async body gets
        `asyncio.CancelledError` at its next `await`. Cancelling a finished
        job changes nothing. Returns `None` when no job has that id, or when it
        has an owner, so a caller cannot tell the two apart.
        """
        return self._cancel(job_id, None)

    def _cancel(self, job_id: str, owner: str | None) -> JobInfo | None:
        """Ask the job `job_id` to stop when `owner` owns it."""
        record = self._find(job_id, owner)
        if record is None:
            return None
        if record.status is JobStatus.QUEUED:
            record.cancel_requested = True
            if record.task is None:
                self._queue.remove(record)
            self._settle(record, JobStatus.CANCELLED)
        elif record.status is JobStatus.RUNNING:
            record.cancel_requested = True
            if self._interruptible and record.task is not None:
                record.task.cancel()
        return _snapshot(self._name, record)

    def _submit(
        self,
        owner: str | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> JobInfo:
        """Record a job for `owner` and start it."""
        try:
            self._signature.bind(None, *args, **kwargs)
        except TypeError as exc:
            msg = f"Job {self._name!r} cannot take these arguments: {exc}"
            raise TypeError(msg) from None
        if self._stopping:
            raise JobRefusedError(
                name=self._name, reason="the tasks are shutting down"
            )
        if self._group is None:
            raise OutOfContextError(self, "submit")
        self._forget_expired()
        if self._running >= self._max_concurrent and (
            self._max_queued is not None
            and len(self._queue) >= self._max_queued
        ):
            raise JobRefusedError(name=self._name, reason="the queue is full")
        record = _Record(
            secrets.token_urlsafe(16), owner, self._clock(), args, kwargs
        )
        self._records[record.id] = record
        self._unfinished.add(record)
        self._queue.append(record)
        self._dispatch()
        return _snapshot(self._name, record)

    def _dispatch(self) -> None:
        """Start queued jobs, oldest first, while a slot is free."""
        group = self._group
        while (
            group is not None
            and not self._stopping
            and self._queue
            and self._running < self._max_concurrent
        ):
            record = self._queue.popleft()
            self._running += 1
            record.task = group.create_task(
                self._run(record), context=record.context
            )
            record.task.add_done_callback(self._release)

    def _release(self, _task: asyncio.Task[None]) -> None:
        """Free the slot of a job task that ended, started or not."""
        self._running -= 1
        self._dispatch()

    def _get(self, job_id: str, owner: str | None) -> JobInfo | None:
        """Return the job `job_id` when `owner` owns it, else `None`."""
        record = self._find(job_id, owner)
        if record is None:
            return None
        return _snapshot(self._name, record)

    def _find(self, job_id: str, owner: str | None) -> _Record | None:
        """Return the record `job_id` when `owner` owns it, else `None`."""
        self._forget_expired()
        record = self._records.get(job_id)
        if record is None or record.owner != owner:
            return None
        return record

    def _settle(self, record: _Record, status: JobStatus) -> None:
        """End `record` with `status`, drop its call, task and context."""
        with record.lock:
            record.status = status
            record.finished_at = self._clock()
        record.args, record.kwargs = (), {}
        record.task = None
        record.context = None
        self._unfinished.discard(record)
        if self._keep is None:
            return
        record.expires_at = monotonic() + self._keep
        self._finished.append(record)
        self._kept.set()
        self._forget_expired()

    async def _sweep(self) -> None:
        """Forget each finished job once its retention ends, until cancelled."""
        finished = self._finished
        while True:
            if finished:
                await sleep(finished[0].expires_at - monotonic())
            else:
                self._kept.clear()
                await self._kept.wait()
            self._forget_expired()

    def _forget_expired(self) -> None:
        """Forget every finished job past its retention.

        Jobs finish in order and share one `keep`, so the oldest expire
        first and the sweep stops at the first one still kept.
        """
        finished = self._finished
        now = monotonic()
        while finished and finished[0].expires_at <= now:
            del self._records[finished.popleft().id]

    async def _run(self, record: _Record) -> None:
        """Run one job's body and settle its record.

        A job that ended before its first step, cancelled while it waited
        to start, settles nothing and never calls the body. A body that
        returns while its task is being cancelled ends `cancelled`. One
        that raises a `BaseException` other than a cancel ends `failed`,
        and the exception propagates, and no queued job starts after it.
        """
        try:
            if record.status is not JobStatus.QUEUED:
                return
            record.status = JobStatus.RUNNING
            record.started_at = self._clock()
            error = await self._fire.run(
                record.started_at,
                None,
                Progress(record, self),
                *record.args,
                **record.kwargs,
            )
            if _cancelling():
                self._settle(record, JobStatus.CANCELLED)
            elif error is None:
                self._settle(record, JobStatus.SUCCEEDED)
            else:
                record.error = _caller_error(error)
                self._settle(record, JobStatus.FAILED)
        except asyncio.CancelledError:
            self._settle(record, JobStatus.CANCELLED)
            raise
        except BaseException:
            self._stopping = True
            record.error = _FAILED
            self._settle(record, JobStatus.FAILED)
            raise


class OwnedJob[**P]:
    """A `Job` as one owner sees it.

    Use `Job.for_owner()` instead of creating one directly.

    Read more in the [Owner](../task.md#owner) docs.
    """

    __slots__ = ("_job", "_owner")

    def __init__(self, job: Job[P], owner: str) -> None:
        self._job = job
        self._owner = owner

    async def submit(self, *args: P.args, **kwargs: P.kwargs) -> JobInfo:
        """Submit a job for this owner, with the function's arguments.

        Returns at once, before the body starts.

        Raises:
            TypeError: If the function cannot take these arguments.
            OutOfContextError: If the tasks never started.
            JobRefusedError: If the queue is full, or the tasks are
                shutting down.
        """
        return self._job._submit(self._owner, args, kwargs)  # noqa: SLF001

    async def get(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Return the job `job_id`.

        Returns `None` when no job has that id, or when another owner, or
        no owner, submitted it, so a caller cannot tell them apart.
        """
        return self._job._get(job_id, self._owner)  # noqa: SLF001

    async def cancel(
        self, job_id: Annotated[str, Doc("The id `submit` returned.")]
    ) -> JobInfo | None:
        """Ask this owner's job `job_id` to stop, and return it at once.

        Returns `None` when no job has that id, or when another owner, or
        no owner, submitted it. Such a job is left as it is.
        """
        return self._job._cancel(job_id, self._owner)  # noqa: SLF001


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


def _resolve(annotation: object, function: Callable[..., Any]) -> object:
    """Resolve an annotation written as a string or a forward reference.

    Raises:
        NameError: If it names something the runtime cannot resolve.
    """
    if isinstance(annotation, str):
        annotation = ForwardRef(annotation, owner=function)
    if isinstance(annotation, ForwardRef):
        return annotation.evaluate(owner=function)
    return annotation


def _is_progress(
    parameter: inspect.Parameter, function: Callable[..., Any]
) -> bool:
    """Return whether `parameter` takes the job's `Progress`."""
    annotation = _resolve(parameter.annotation, function)
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
        annotation = _resolve(parameter.annotation, function)
    except NameError:
        return False
    return get_origin(annotation) is Annotated and any(
        isinstance(marker, markers) for marker in get_args(annotation)[1:]
    )


def _caller_error(error: Exception) -> str:
    """Return the error a caller sees for `error`.

    A `JobError`, or a group made only of them, shows its first message.
    Anything else shows as `"Job failed"`.
    """
    if isinstance(error, ExceptionGroup):
        match, rest = error.split(JobError)
        if match is not None and rest is None:
            error = _first_leaf(match)
    if isinstance(error, JobError):
        return str(error) or _FAILED
    return _FAILED


def _first_leaf(group: ExceptionGroup[Exception]) -> Exception:
    """Return the first exception in `group` that is not a group."""
    first = group.exceptions[0]
    return _first_leaf(first) if isinstance(first, ExceptionGroup) else first


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


def _snapshot(name: str, record: _Record) -> JobInfo:
    """Return a `JobInfo` of `record` as it is now.

    The counts, results and status are read together, so a sync body
    reporting from its thread never shows half of one report.
    """
    with record.lock:
        status, done, total = record.status, record.done, record.total
        results, finished_at = tuple(record.results), record.finished_at
    return JobInfo(
        id=record.id,
        name=name,
        status=status,
        done=done,
        total=total,
        results=results,
        error=record.error,
        cancel_requested=record.cancel_requested,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=finished_at,
    )
