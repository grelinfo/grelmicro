"""Test the trace link between a job's submitter and its run."""

import asyncio
import logging
import time
from collections.abc import Callable, Generator
from datetime import timedelta
from typing import Any

import pytest
from opentelemetry import baggage, context, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import BaseModel, field_validator

from grelmicro.clock import VirtualClock
from grelmicro.task import Job, JobError, JobInfo, JobStatus, Progress, Tasks
from grelmicro.task import _job as job_module
from grelmicro.task import _job_trace as job_trace
from grelmicro.task._protocol import Settled
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]

_SPAN = "job tests.task.test_job_trace:{}"


@pytest.fixture
def spans() -> Generator[InMemorySpanExporter]:
    """Capture spans on a global tracer provider of the test's own."""
    saved = trace._TRACER_PROVIDER
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = saved


async def _ended(job: Job[...], job_id: str) -> JobInfo:
    """Poll `job_id` until it reaches an end state, and return it."""
    while True:
        info = await job.without_owner().get(job_id)
        assert info is not None
        if info.finished_at is not None:
            return info
        await asyncio.sleep(0)


def _run_span(spans: InMemorySpanExporter, function: str) -> ReadableSpan:
    """Return the one span of a run of `function`."""
    found = [
        span
        for span in spans.get_finished_spans()
        if span.name == _SPAN.format(function)
    ]
    assert len(found) == 1
    return found[0]


async def _quiet(progress: Progress) -> None:
    """Do nothing."""
    _ = progress


async def test_job_run_span_links_to_submitter_in_its_own_trace(
    spans: InMemorySpanExporter,
) -> None:
    """The run has its own trace, linked to the span that submitted it."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_quiet)
    tracer = trace.get_tracer("test")

    # Act
    async with tasks:
        with tracer.start_as_current_span("request") as request:
            info = await job.without_owner().submit()
        await _ended(job, info.id)

    # Assert
    run = _run_span(spans, "_quiet")
    submitter = request.get_span_context()
    assert run.parent is None
    assert run.kind is SpanKind.CONSUMER
    assert run.context.trace_id != submitter.trace_id
    assert [link.context.trace_id for link in run.links] == [submitter.trace_id]
    assert [link.context.span_id for link in run.links] == [submitter.span_id]
    assert run.attributes is not None
    assert run.attributes["grelmicro.task.name"] == job.name
    assert "grelmicro.task.job.id" not in run.attributes
    assert info.id not in run.attributes.values()


async def _blocks(progress: Progress, key: str) -> None:
    """Wait for the event kept under `key`."""
    _ = progress
    await shared(key).wait()


async def test_job_run_span_links_when_the_job_was_queued(
    spans: InMemorySpanExporter,
) -> None:
    """A job claimed later links to its submitter, read from the store."""
    # Arrange
    release = asyncio.Event()
    key = share(release)
    tasks = Tasks()
    job = tasks.job(name="queued", max_concurrent=1)(_blocks)
    tracer = trace.get_tracer("test")

    # Act
    async with tasks:
        first = await job.without_owner().submit(key)
        with tracer.start_as_current_span("request") as request:
            second = await job.without_owner().submit(key)
        assert second.status is JobStatus.QUEUED
        release.set()
        await _ended(job, first.id)
        await _ended(job, second.id)

    # Assert
    first_run, second_run = [
        span for span in spans.get_finished_spans() if span.name == "job queued"
    ]
    assert [link.context.span_id for link in second_run.links] == [
        request.get_span_context().span_id
    ]
    assert not first_run.links


async def test_job_run_without_a_submitter_span_has_no_link(
    spans: InMemorySpanExporter,
) -> None:
    """A job submitted outside any span still has its span, with no link."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_quiet)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        await _ended(job, info.id)

    # Assert
    assert not _run_span(spans, "_quiet").links


async def test_job_run_is_no_child_of_a_span_open_at_startup(
    spans: InMemorySpanExporter,
) -> None:
    """A span open when the tasks started does not become the run's parent."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_quiet)
    tracer = trace.get_tracer("test")

    # Act
    with tracer.start_as_current_span("startup"):
        async with tasks:
            info = await job.without_owner().submit()
            await _ended(job, info.id)

    # Assert
    assert _run_span(spans, "_quiet").parent is None


async def _current_trace(progress: Progress) -> str:
    """Return the trace id the body runs in."""
    _ = progress
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def _current_trace_sync(progress: Progress) -> str:
    """Return the trace id a sync body runs in."""
    _ = progress
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


@pytest.mark.parametrize(
    ("function", "name"),
    [
        (_current_trace, "_current_trace"),
        (_current_trace_sync, "_current_trace_sync"),
    ],
)
async def test_job_body_runs_inside_the_run_span(
    spans: InMemorySpanExporter, function: Callable[..., object], name: str
) -> None:
    """Spans a body opens, async or sync, join the run's trace."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(function)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        ended = await _ended(job, info.id)

    # Assert
    assert ended.result == format(
        _run_span(spans, name).context.trace_id, "032x"
    )


async def _succeeds(progress: Progress) -> str:
    """Return JSON."""
    _ = progress
    return "done"


async def _raises(progress: Progress) -> None:
    """Fail with an unexpected error."""
    _ = progress
    msg = "unexpected"
    raise RuntimeError(msg)


async def _refuses(progress: Progress) -> None:
    """Fail with a message for the caller."""
    _ = progress
    msg = "Row 3 is invalid"
    raise JobError(msg)


async def _not_json(progress: Progress) -> object:
    """Return something that is not JSON."""
    _ = progress
    return object()


async def test_job_run_span_of_a_success_is_not_an_error(
    spans: InMemorySpanExporter,
) -> None:
    """A job that succeeds leaves the span's status alone."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_succeeds)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        await _ended(job, info.id)

    # Assert
    assert _run_span(spans, "_succeeds").status.status_code is StatusCode.UNSET


async def _refuses_in_a_group(progress: Progress) -> None:
    """Fail with a group holding only messages for the caller."""
    _ = progress
    msg = "Row 3 is invalid"
    group = "rows"
    raise ExceptionGroup(group, [JobError(msg)])


async def test_job_run_span_records_an_unexpected_failure(
    spans: InMemorySpanExporter,
) -> None:
    """A body that raises marks the span, names the type, and records it."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_raises)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        ended = await _ended(job, info.id)

    # Assert
    assert ended.status is JobStatus.FAILED
    run = _run_span(spans, "_raises")
    assert run.status.status_code is StatusCode.ERROR
    assert run.status.description == "Job failed"
    assert run.attributes is not None
    assert run.attributes["error.type"] == "RuntimeError"
    assert [event.name for event in run.events] == ["exception"]


@pytest.mark.parametrize(
    ("function", "name", "error_type"),
    [
        (_refuses, "_refuses", "JobError"),
        (_refuses_in_a_group, "_refuses_in_a_group", "ExceptionGroup"),
    ],
)
async def test_job_run_span_records_no_event_for_a_job_error(
    spans: InMemorySpanExporter,
    function: Callable[..., object],
    name: str,
    error_type: str,
) -> None:
    """A `JobError` is an expected failure, so the span gets no traceback."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(function)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        ended = await _ended(job, info.id)

    # Assert
    assert ended.status is JobStatus.FAILED
    run = _run_span(spans, name)
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None
    assert run.attributes["error.type"] == error_type
    assert not run.events


@pytest.mark.parametrize(
    ("signal", "events"),
    [
        ("logs", []),
        ("LOGS ", []),
        ("logs/dup", ["exception"]),
        ("", ["exception"]),
    ],
)
async def test_job_run_span_honours_the_exception_signal_opt_in(
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
    events: list[str],
) -> None:
    """`logs` leaves the event off the span, and the failure still shows."""
    # Arrange
    monkeypatch.setenv("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", signal)
    tasks = Tasks()
    job = tasks.job()(_raises)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        await _ended(job, info.id)

    # Assert
    run = _run_span(spans, "_raises")
    assert [event.name for event in run.events] == events
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None
    assert run.attributes["error.type"] == "RuntimeError"


async def _takes_an_int(progress: Progress, count: int) -> None:
    """Take an int."""
    _ = progress, count


async def test_job_run_span_of_stale_arguments_holds_no_value(
    spans: InMemorySpanExporter,
) -> None:
    """Arguments that no longer match name the type, never the value."""
    # Arrange
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_takes_an_int)

    # Act
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
        for _ in range(2):
            await clock.advance(1)

    # Assert
    run = _run_span(spans, "_takes_an_int")
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None
    assert run.attributes["error.type"] == "ValidationError"
    assert not run.events


class _Touchy(BaseModel):
    """A model whose validator raises what Pydantic does not wrap."""

    a: int

    @field_validator("a", mode="before")
    @classmethod
    def _refuse(cls, value: Any) -> int:  # noqa: ANN401
        msg = f"refused {value}"
        raise TypeError(msg)


async def _takes_a_touchy(progress: Progress, row: _Touchy) -> None:
    """Take a model that raises while it loads."""
    _ = progress, row


async def _reports_it_ran(progress: Progress, key: str) -> None:
    """Report that it ran."""
    _ = progress
    shared(key).set()


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        pytest.param(
            '{"row": {"a": "a-stored-secret"}}',
            "TypeError",
            id="validator-raises",
        ),
        pytest.param("a-stored-secret", "JSONDecodeError", id="broken-json"),
    ],
)
async def test_job_arguments_that_cannot_load_fail_only_that_job(
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
    payload: str,
    error_type: str,
) -> None:
    """Whatever loading raises ends one job, and the tasks keep running."""
    # Arrange
    caplog.set_level(logging.DEBUG, logger="grelmicro.task")
    tasks = Tasks()
    job = tasks.job()(_takes_a_touchy)
    other = tasks.job()(_reports_it_ran)
    ran = asyncio.Event()

    # Act
    async with tasks:
        await job._backend.submit(
            name=job.name,
            job_id="broken",
            owner=None,
            payload=payload,
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
        info = await _ended(job, "broken")
        await other.without_owner().submit(share(ran))
        await asyncio.wait_for(ran.wait(), 5)

    # Assert
    assert (info.status, info.error) == (JobStatus.FAILED, "Job failed")
    run = _run_span(spans, "_takes_a_touchy")
    assert run.status.description == "Job failed"
    assert run.attributes is not None
    assert run.attributes["error.type"] == error_type
    assert not run.events
    assert "a-stored-secret" not in caplog.text


async def _blocks_the_loop(progress: Progress) -> None:
    """Hold the event loop for longer than the lease."""
    _ = progress
    time.sleep(0.05)  # noqa: ASYNC251


async def test_job_lost_before_it_settles_is_logged_and_marked(
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job the store no longer gives this replica says so, in log and span."""
    # Arrange
    monkeypatch.setattr(job_module, "LEASE", timedelta(milliseconds=10))
    caplog.set_level(logging.WARNING, logger="grelmicro.task")
    tasks = Tasks()
    job = tasks.job()(_blocks_the_loop)

    # Act
    async with tasks:
        submitted = await job.without_owner().submit()
        info = await _ended(job, submitted.id)

    # Assert
    assert (info.status, info.error) == (JobStatus.FAILED, "Job lost")
    run = _run_span(spans, "_blocks_the_loop")
    assert run.status.status_code is StatusCode.ERROR
    assert run.status.description == "Job lost"
    assert run.attributes is not None
    assert run.attributes["error.type"] == "job_lost"
    [record] = [r for r in caplog.records if r.name == "grelmicro.task"]
    assert record.levelno == logging.WARNING
    assert job.name in record.getMessage()
    assert submitted.id not in record.getMessage()


async def _runs_on(progress: Progress) -> None:
    """Run until cancelled."""
    _ = progress
    await asyncio.Event().wait()


async def test_job_past_max_run_is_logged_and_marked_timed_out(
    spans: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    """A job stopped by `max_run` says so, in log and span."""
    # Arrange
    caplog.set_level(logging.WARNING, logger="grelmicro.task")
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job(max_run=5)(_runs_on)

    # Act
    async with clock, tasks:
        submitted = await job.without_owner().submit()
        for _ in range(7):
            await clock.advance(1)
            for _ in range(5):
                await asyncio.sleep(0)
        while job._runs:  # noqa: ASYNC110
            await asyncio.sleep(0)

    # Assert
    run = _run_span(spans, "_runs_on")
    assert run.status.status_code is StatusCode.ERROR
    assert run.status.description == "Job not finished in time"
    assert run.attributes is not None
    assert run.attributes["error.type"] == "job_timed_out"
    [record] = [r for r in caplog.records if r.name == "grelmicro.task"]
    assert record.levelno == logging.WARNING
    assert "max_run" in record.getMessage()
    assert submitted.id not in record.getMessage()


class _Abort(BaseException):
    """An error that is not an `Exception`."""


async def _abort(progress: Progress) -> None:
    """Raise an error that is not an `Exception`."""
    _ = progress
    raise _Abort


async def test_job_run_span_of_a_base_exception_names_its_type(
    spans: InMemorySpanExporter,
) -> None:
    """A body raising a `BaseException` marks the span and names the type."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_abort)

    # Act
    with pytest.RaisesGroup(_Abort, flatten_subgroups=True):
        async with tasks:
            await job.without_owner().submit()
            await asyncio.Event().wait()

    # Assert
    run = _run_span(spans, "_abort")
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None
    assert run.attributes["error.type"] == "_Abort"


async def test_job_run_span_of_a_non_json_return_is_an_error(
    spans: InMemorySpanExporter,
) -> None:
    """A job that fails after its body returned still marks the span."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_not_json)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        ended = await _ended(job, info.id)

    # Assert
    assert ended.status is JobStatus.FAILED
    assert _run_span(spans, "_not_json").status.status_code is StatusCode.ERROR


async def _waits(progress: Progress, key: str) -> None:
    """Set the event kept under `key`, then wait for a cancel."""
    _ = progress
    shared(key).set()
    await asyncio.Event().wait()


async def test_job_run_span_of_a_cancel_is_not_an_error(
    spans: InMemorySpanExporter,
) -> None:
    """A cancel is what the caller asked for, so the span reads no error."""
    # Arrange
    started = asyncio.Event()
    key = share(started)
    tasks = Tasks()
    job = tasks.job(name="waits")(_waits)

    # Act
    async with tasks:
        info = await job.without_owner().submit(key)
        await started.wait()
        await job.without_owner().cancel(info.id)
        ended = await _ended(job, info.id)

    # Assert
    assert ended.status is JobStatus.CANCELLED
    found = [s for s in spans.get_finished_spans() if s.name == "job waits"]
    assert [s.status.status_code for s in found] == [StatusCode.UNSET]
    assert not found[0].events
    assert found[0].attributes is not None
    assert "error.type" not in found[0].attributes


@pytest.mark.parametrize("signal", ["", "logs"])
async def test_job_run_span_of_a_store_failure_holds_no_message(
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """A job whose end the store never took reads lost, without the error."""
    # Arrange
    monkeypatch.setenv("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", signal)

    async def _fails(_self: object, **_kwargs: object) -> None:
        msg = "store-url-with-password"
        raise RuntimeError(msg)

    monkeypatch.setattr(MemoryJobAdapter, "settle", _fails)
    clock = VirtualClock()
    tasks = Tasks()
    job = tasks.job()(_succeeds)

    # Act
    async with clock, tasks:
        await job.without_owner().submit()
        for _ in range(31):
            await clock.advance(1)
            for _ in range(10):
                await asyncio.sleep(0)

    # Assert
    run = _run_span(spans, "_succeeds")
    assert run.status.status_code is StatusCode.ERROR
    assert run.status.description == "Job lost"
    assert run.attributes is not None
    assert run.attributes["error.type"] == "job_lost"
    assert not run.events
    assert "store-url-with-password" not in str(run.to_json())


async def test_job_settle_runs_inside_the_run_span(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store call that ends a job joins the run's trace."""
    # Arrange
    seen: list[int] = []
    settle: Any = MemoryJobAdapter.settle

    async def _spying(self: MemoryJobAdapter, **kwargs: object) -> Settled:
        seen.append(trace.get_current_span().get_span_context().trace_id)
        return await settle(self, **kwargs)

    monkeypatch.setattr(MemoryJobAdapter, "settle", _spying)
    tasks = Tasks()
    job = tasks.job()(_quiet)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        await _ended(job, info.id)

    # Assert
    assert seen == [_run_span(spans, "_quiet").context.trace_id]


@pytest.mark.usefixtures("spans")
async def test_job_stored_context_is_the_trace_context_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baggage stays out of the store, whatever the app's propagators."""
    # Arrange
    stored: list[dict[str, str]] = []
    submit: Any = MemoryJobAdapter.submit

    async def _spying(self: MemoryJobAdapter, **kwargs: Any) -> object:  # noqa: ANN401
        stored.append(dict(kwargs["context"]))
        return await submit(self, **kwargs)

    monkeypatch.setattr(MemoryJobAdapter, "submit", _spying)
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", W3CBaggagePropagator())
    tasks = Tasks()
    job = tasks.job()(_quiet)
    tracer = trace.get_tracer("test")

    # Act
    token = context.attach(baggage.set_baggage("user", "someone@example.com"))
    try:
        async with tasks:
            with tracer.start_as_current_span("request"):
                info = await job.without_owner().submit()
            await _ended(job, info.id)
    finally:
        context.detach(token)

    # Assert
    assert [set(entry) for entry in stored] == [{"traceparent"}]


def test_job_broken_stored_context_starts_no_link(
    spans: InMemorySpanExporter,
) -> None:
    """Text in the store that is no trace context means no link, no error."""
    # Act
    with job_trace.run_span("name", {"traceparent": "not-a-trace"}):
        pass

    # Assert
    finished = spans.get_finished_spans()
    assert len(finished) == 1
    assert not finished[0].links


async def test_job_runs_without_opentelemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the package, a job stores no context and still runs."""
    # Arrange
    monkeypatch.setattr(job_trace, "_otel", lambda: None)
    tasks = Tasks()
    job = tasks.job()(_raises)

    # Act
    async with tasks:
        info = await job.without_owner().submit()
        ended = await _ended(job, info.id)

    # Assert
    assert ended.status is JobStatus.FAILED
    assert job_trace.submitter_context() == {}


async def test_job_lost_without_opentelemetry_still_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without the package, a lost job still reads lost and logs a warning."""
    # Arrange
    monkeypatch.setattr(job_trace, "_otel", lambda: None)
    monkeypatch.setattr(job_module, "LEASE", timedelta(milliseconds=10))
    caplog.set_level(logging.WARNING, logger="grelmicro.task")
    tasks = Tasks()
    job = tasks.job()(_blocks_the_loop)

    # Act
    async with tasks:
        submitted = await job.without_owner().submit()
        info = await _ended(job, submitted.id)

    # Assert
    assert info.error == "Job lost"
    assert "Job lost" in caplog.text
