"""Test the Memory job backend's invariants under any order of operations.

Memory only: this moves time with a virtual clock, lands on exact deadlines, and
checks exact `purge` counts. The contract suite is what every store runs.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from grelmicro.clock import VirtualClock
from grelmicro.task import JobStatus
from grelmicro.task._protocol import (
    JOB_LOST,
    JOB_NOT_STARTED,
    Beat,
    JobRecord,
    ProgressUpdate,
)
from grelmicro.task.memory import MemoryJobAdapter

NAME = "app:job"
OWNER = "alice"
LEASE = timedelta(seconds=30)
KEEP = timedelta(seconds=120)
MAX_WAIT = timedelta(seconds=90)
MAX_QUEUED = 2
JOBS = ("j0", "j1", "j2")
RUNNERS = ("r0", "r1")
HOLDER = "the job's holder"
END_STATES = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
ORDER = {
    JobStatus.QUEUED: 0,
    JobStatus.RUNNING: 1,
    JobStatus.SUCCEEDED: 2,
    JobStatus.FAILED: 2,
    JobStatus.CANCELLED: 2,
}

_job = st.sampled_from(JOBS)
_runner = st.sampled_from(RUNNERS)
_writer = st.sampled_from((*RUNNERS, HOLDER, HOLDER))
_operation = st.one_of(
    st.tuples(st.just("submit"), _job, st.none() | _runner),
    st.tuples(st.just("claim"), _runner, st.integers(1, 3)),
    st.tuples(st.just("beat"), _writer, _job, st.integers(0, 3)),
    st.tuples(st.just("settle"), _writer, _job, st.sampled_from(END_STATES)),
    st.tuples(st.just("cancel"), _job),
    st.tuples(st.just("elapse"), st.sampled_from((1, 10, 30, 90, 120))),
    st.tuples(st.just("reap")),
    st.tuples(st.just("purge")),
    st.tuples(st.just("read")),
)


@dataclass
class _Model:
    """What the invariants remember across operations."""

    seen: dict[str, JobRecord] = field(default_factory=dict)
    claims: dict[str, list[str]] = field(default_factory=dict)
    submitted: set[str] = field(default_factory=set)
    started_at_submit: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)

    def holder(self, job_id: str) -> str | None:
        """Return the only runner that ever held the job."""
        claims = self.claims.get(job_id)
        return claims[0] if claims else None

    def writer(self, runner: str, job_id: str) -> str:
        """Resolve `HOLDER` to the job's holder, when it has one."""
        return (
            (self.holder(job_id) or RUNNERS[0]) if runner == HOLDER else runner
        )


async def _apply(  # noqa: C901, PLR0912, PLR0915
    backend: MemoryJobAdapter,
    clock: VirtualClock,
    model: _Model,
    operation: tuple,
) -> None:
    """Run one operation, recording what the invariants need."""
    match operation:
        case ("submit", job_id, start_as):
            # A retry is only safe while the job is kept, so never retry one
            # that may be gone.
            if job_id in model.submitted and (
                await backend.get(name=NAME, job_id=job_id, owner=OWNER) is None
            ):
                model.gone.add(job_id)
                return
            if job_id in model.gone:
                return
            submitted = await backend.submit(
                name=NAME,
                job_id=job_id,
                owner=OWNER,
                payload="{}",
                context={},
                max_queued=MAX_QUEUED,
                max_wait=MAX_WAIT,
                max_run=None,
                keep=KEEP,
                max_kept_succeeded=None,
                max_kept_failed=None,
                start_as=start_as,
                lease=LEASE,
            )
            if submitted.record is not None:
                model.submitted.add(job_id)
            if submitted.started:
                assert start_as is not None
                if job_id in model.claims:
                    assert job_id in model.started_at_submit, "started twice"
                    assert model.holder(job_id) == start_as, "started twice"
                else:
                    model.claims[job_id] = [start_as]
                    model.started_at_submit.add(job_id)
        case ("claim", runner, limit):
            for job in await backend.claim(
                slots={NAME: limit}, runner=runner, lease=LEASE
            ):
                model.claims.setdefault(job.record.id, []).append(runner)
        case ("beat", writer, job_id, done):
            runner = model.writer(writer, job_id)
            before = await backend.get(name=NAME, job_id=job_id, owner=OWNER)
            [reply] = await backend.beat(
                runner=runner,
                beats=[Beat(NAME, job_id, ProgressUpdate(done=done, total=3))],
                lease=LEASE,
            )
            if reply.held:
                assert runner == model.holder(job_id), (
                    "fence: a beat got through"
                )
                assert before is not None
                assert before.status is JobStatus.RUNNING
            else:
                after = await backend.get(name=NAME, job_id=job_id, owner=OWNER)
                assert after == before, "a refused beat wrote"
        case ("settle", writer, job_id, status):
            runner = model.writer(writer, job_id)
            before = await backend.get(name=NAME, job_id=job_id, owner=OWNER)
            settled = await backend.settle(
                name=NAME,
                job_id=job_id,
                runner=runner,
                status=status,
                error=None,
                result='"done"',
                progress=None,
            )
            after = await backend.get(name=NAME, job_id=job_id, owner=OWNER)
            if not settled.settled:
                assert after == before, "a refused settle wrote"
                return
            assert runner == model.holder(job_id), "fence: a settle got through"
            assert before is not None
            assert before.error not in (JOB_LOST, JOB_NOT_STARTED), (
                "fence: settled a lost job"
            )
            if before.status is not JobStatus.RUNNING:
                assert after == before, "a settle retry wrote"
        case ("cancel", job_id):
            await backend.cancel(name=NAME, job_id=job_id, owner=OWNER)
        case ("elapse", seconds):
            await clock.advance(seconds)
        case ("reap",):
            await backend.reap(limit=10)
        case ("purge",):
            await backend.purge(limit=10)
        case ("read",):
            await _check(backend, model)


async def _check(backend: MemoryJobAdapter, model: _Model) -> list[JobRecord]:
    """Check every invariant on what a reader sees now, and return it."""
    readable: list[JobRecord] = []
    queued = 0
    for job_id in JOBS:
        read = await backend.get(name=NAME, job_id=job_id, owner=OWNER)
        previous = model.seen.get(job_id)
        if read is None:
            if job_id in model.submitted:
                model.gone.add(job_id)
            continue
        readable.append(read)
        assert job_id not in model.gone, "a forgotten job came back"
        assert len(model.claims.get(job_id, [])) <= 1, "claimed twice"
        queued += read.status is JobStatus.QUEUED
        if read.status is JobStatus.QUEUED:
            assert read.started_at is None
        if read.status in END_STATES:
            assert read.finished_at is not None
        if previous is None:
            model.seen[job_id] = read
            continue
        assert ORDER[read.status] >= ORDER[previous.status], "moved backward"
        if previous.status in END_STATES:
            assert read == previous, "an ended job changed"
        assert read.version >= previous.version, "the version went backward"
        if read != previous:
            assert read.version > previous.version, "changed, same version"
        model.seen[job_id] = read
    assert queued <= MAX_QUEUED, "past max_queued"
    return readable


async def _run(backend: MemoryJobAdapter, operations: list[tuple]) -> None:
    """Run the operations in order, then check reap and purge count exactly."""
    clock = VirtualClock()
    model = _Model()
    async with clock, backend:
        for operation in operations:
            await _apply(backend, clock, model, operation)
        readable = await _check(backend, model)
        unended = [r for r in readable if r.status not in END_STATES]

        await clock.advance((LEASE + MAX_WAIT).total_seconds())
        assert await backend.reap(limit=len(JOBS)) == len(unended), "reap count"
        assert await backend.reap(limit=len(JOBS)) == 0, "reaped twice"

        await clock.advance(KEEP.total_seconds())
        assert await backend.purge(limit=len(JOBS)) == len(readable), "purge"
        assert await backend.purge(limit=len(JOBS)) == 0, "purged twice"


@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(st.lists(_operation, max_size=40))
@example(
    [
        ("submit", "j1", None),
        ("submit", "j0", "r0"),
        ("claim", "r0", 2),
        ("submit", "j0", "r0"),
    ]
)
@example(
    [
        ("submit", "j0", "r0"),
        ("settle", HOLDER, "j0", JobStatus.SUCCEEDED),
        ("settle", HOLDER, "j0", JobStatus.SUCCEEDED),
    ]
)
def test_job_backend_keeps_its_invariants_in_any_order(
    operations: list[tuple],
) -> None:
    """A job only moves forward, is claimed once, and refused writes write nothing."""
    # Arrange
    backend = MemoryJobAdapter()

    # Act & Assert
    asyncio.run(_run(backend, operations))
