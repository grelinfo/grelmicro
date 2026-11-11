"""Test the contract every job backend keeps."""

import asyncio
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from grelmicro.clock import VirtualClock
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import (
    JOB_LOST,
    JOB_NOT_STARTED,
    Beat,
    EndStatus,
    JobBackend,
    JobRecord,
    ProgressUpdate,
)
from grelmicro.task.memory import MemoryJobAdapter
from tests._redis_cluster import CLUSTERS, cluster_provider, hash_tag_prefix
from tests._redis_sentinel import SentinelSet

pytestmark = [pytest.mark.timeout(30, func_only=True)]
"""Thirty seconds of test body.

`func_only` keeps container startup out of the budget.
"""

NAME = "app:import_rows"
OWNER = "alice"


LEASE_TICKS = 30
"""A job's lease in the tests, in ticks."""

MARGIN = 5
"""Ticks kept clear of every deadline, since no real clock hits one exactly."""


@dataclass
class _Store:
    """A backend under test, and how to move its time forward.

    Time is counted in ticks, so a store on a real clock runs the same tests
    with a short tick and real sleeps.
    """

    backend: JobBackend
    advance: Callable[[float], Awaitable[object]]
    tick: timedelta = timedelta(seconds=1)

    @property
    def lease(self) -> timedelta:
        """Return the lease the tests use."""
        return self.ticks(LEASE_TICKS)

    def ticks(self, count: int) -> timedelta:
        """Return `count` ticks as a duration."""
        return self.tick * count

    async def elapse(self, ticks: int) -> None:
        """Move the store's time forward by `ticks`."""
        await self.advance(self.ticks(ticks).total_seconds())

    def args(self, **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
        """Return the arguments of a `submit` that stores a plain queued job."""
        return {
            "name": NAME,
            "job_id": secrets.token_urlsafe(8),
            "owner": OWNER,
            "payload": "{}",
            "context": {},
            "max_queued": None,
            "max_wait": None,
            "keep": timedelta(hours=1),
            "start_as": None,
            "lease": self.lease,
            **overrides,
        }

    async def submit(self, **overrides: Any) -> JobRecord:  # noqa: ANN401
        """Store a queued job, and return its record."""
        submitted = await self.backend.submit(**self.args(**overrides))
        assert submitted.record is not None
        return submitted.record


REAL_TICK = timedelta(milliseconds=50)
"""A tick on a store with a real clock, so a lease is a second and a half."""


@pytest.fixture(scope="module")
def redis_url() -> Generator[str]:
    """Start one Redis for the module, and return its URL."""
    with RedisContainer() as container:
        yield f"redis://localhost:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="module")
def valkey_url() -> Generator[str]:
    """Start one Valkey for the module, and return its URL."""
    with RedisContainer(image="valkey/valkey:8.1") as container:
        yield f"redis://localhost:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="module")
def postgres_url() -> Generator[str]:
    """Start one Postgres for the module, and return its URL."""
    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        yield f"postgresql://test:test@localhost:{port}/test"


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.integration]),
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("valkey", marks=[pytest.mark.integration]),
        pytest.param("redis-sentinel", marks=[pytest.mark.integration]),
        *(
            pytest.param(name, marks=[pytest.mark.integration])
            for name in CLUSTERS
        ),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncGenerator[_Store]:
    """Provide each job backend, open."""
    if request.param in {"redis", "valkey", "redis-sentinel", *CLUSTERS}:
        if request.param == "redis":
            provider = RedisProvider(request.getfixturevalue("redis_url"))
        elif request.param == "redis-sentinel":
            sentinel_set: SentinelSet = request.getfixturevalue(
                "redis_sentinel"
            )
            provider = sentinel_set.provider()
        elif request.param == "valkey":
            provider = ValkeyProvider(request.getfixturevalue("valkey_url"))
        else:
            provider = cluster_provider(request, request.param)
        adapter = provider.job_backend(prefix=hash_tag_prefix())
        async with provider, adapter:
            yield _Store(adapter, asyncio.sleep, REAL_TICK)
        return
    if request.param == "postgres":
        postgres = PostgresProvider(request.getfixturevalue("postgres_url"))
        table_name = f"jobs_{secrets.token_hex(6)}"
        async with postgres, postgres.job_backend(table_name=table_name) as pg:
            yield _Store(pg, asyncio.sleep, REAL_TICK)
            await postgres.client.execute(f"DROP TABLE IF EXISTS {table_name}")
        return
    if request.param == "sqlite":
        path = request.getfixturevalue("tmp_path") / "jobs.db"
        sqlite = SQLiteProvider(path)
        async with sqlite, sqlite.job_backend() as adapter:
            yield _Store(adapter, asyncio.sleep, REAL_TICK)
        return
    backend = MemoryJobAdapter()
    clock = VirtualClock()
    async with clock, backend:
        yield _Store(backend, clock.advance)


async def test_job_backend_submit_stores_a_queued_job(store: _Store) -> None:
    """A submitted job reads back `queued`, at version 1."""
    # Arrange
    arguments = store.args()

    # Act
    submitted = await store.backend.submit(**arguments)
    record = await store.backend.get(
        name=NAME, job_id=arguments["job_id"], owner=OWNER
    )

    # Assert
    assert submitted.started is False
    assert record is not None
    assert record == submitted.record
    assert record.id == arguments["job_id"]
    assert record.name == NAME
    assert record.status is JobStatus.QUEUED
    assert record.version == 1
    assert (record.done, record.total, record.result) == (0, None, None)
    assert record.error is None
    assert record.cancel_requested is False
    assert record.started_at is None
    assert record.finished_at is None


async def test_job_backend_submit_twice_keeps_one_job(store: _Store) -> None:
    """A retried `submit` with the same id returns the job it already stored."""
    # Arrange
    arguments = store.args()
    first = await store.backend.submit(**arguments)

    # Act
    second = await store.backend.submit(**arguments)

    # Assert
    assert second.record == first.record
    claimed = await store.backend.claim(
        slots={NAME: 10}, runner="r", lease=timedelta(seconds=30)
    )
    assert len(claimed) == 1


@pytest.mark.parametrize(
    ("submitted_by", "read_by", "found"),
    [
        pytest.param("alice", "alice", True, id="owner"),
        pytest.param(None, None, True, id="no-owner"),
        pytest.param("", "", True, id="empty-owner"),
        pytest.param("alice", "bob", False, id="another-owner"),
        pytest.param("alice", None, False, id="owned-read-without-owner"),
        pytest.param(None, "alice", False, id="unowned-read-with-owner"),
        pytest.param("", None, False, id="empty-owner-is-not-no-owner"),
    ],
)
async def test_job_backend_get_finds_a_job_only_for_its_owner(
    store: _Store,
    submitted_by: str | None,
    read_by: str | None,
    found: bool,  # noqa: FBT001
) -> None:
    """A job of another owner reads exactly like a missing id."""
    # Arrange
    record = await store.submit(owner=submitted_by)

    # Act
    read = await store.backend.get(name=NAME, job_id=record.id, owner=read_by)

    # Assert
    assert (read is not None) is found


async def test_job_backend_get_is_scoped_to_the_job_name(store: _Store) -> None:
    """A job of another name, and an unknown id, read as not found."""
    # Arrange
    record = await store.submit()

    # Act
    other_name = await store.backend.get(
        name="app:other", job_id=record.id, owner=OWNER
    )
    unknown = await store.backend.get(name=NAME, job_id="nope", owner=OWNER)

    # Assert
    assert other_name is None
    assert unknown is None


async def _claim(
    store: _Store, runner: str = "r", limit: int = 10
) -> list[str]:
    """Claim queued jobs of `NAME`, and return their ids."""
    claimed = await store.backend.claim(
        slots={NAME: limit}, runner=runner, lease=store.lease
    )
    return [job.record.id for job in claimed]


async def test_job_backend_claim_returns_the_oldest_first(
    store: _Store,
) -> None:
    """Claims take jobs in submission order, up to `limit`."""
    # Arrange
    first = await store.submit()
    second = await store.submit()
    third = await store.submit()

    # Act
    claimed = await _claim(store, limit=2)
    rest = await _claim(store)

    # Assert
    assert claimed == [first.id, second.id]
    assert rest == [third.id]


async def test_job_backend_claim_moves_a_job_to_running(store: _Store) -> None:
    """A claimed job reads `running`, and hands the runner its call."""
    # Arrange
    record = await store.submit(payload='{"a": 1}', context={"t": "x"})

    # Act
    [claimed] = await store.backend.claim(
        slots={NAME: 1}, runner="r", lease=store.lease
    )
    read = await store.backend.get(name=NAME, job_id=record.id, owner=OWNER)

    # Assert
    assert (claimed.payload, claimed.context) == ('{"a": 1}', {"t": "x"})
    assert claimed.record == read
    assert read is not None
    assert read.status is JobStatus.RUNNING
    assert read.version == record.version + 1
    assert read.started_at is not None


async def test_job_backend_claim_takes_a_job_at_most_once(
    store: _Store,
) -> None:
    """Two runners racing for one job never both get it."""
    # Arrange
    await store.submit()

    # Act
    first, second = await asyncio.gather(
        _claim(store, "r1"), _claim(store, "r2")
    )

    # Assert
    assert sorted([len(first), len(second)]) == [0, 1]


async def test_job_backend_claim_gives_each_name_its_own_slots(
    store: _Store,
) -> None:
    """A claim takes up to each name's own number, whatever the others have."""
    # Arrange
    other = "app:other"
    await store.submit()
    await store.submit()
    await store.submit(name=other)
    await store.submit(name=other)

    # Act
    claimed = await store.backend.claim(
        slots={NAME: 1, other: 2}, runner="r", lease=store.lease
    )

    # Assert
    names = [job.record.name for job in claimed]
    assert (names.count(NAME), names.count(other)) == (1, 2)


async def test_job_backend_beat_covers_jobs_of_several_names(
    store: _Store,
) -> None:
    """One beat covers every job a replica holds, whatever its name."""
    # Arrange
    other = "app:other"
    first = await _running(store)
    await store.submit(name=other)
    [second] = await store.backend.claim(
        slots={other: 1}, runner="r", lease=store.lease
    )

    # Act
    replies = await store.backend.beat(
        runner="r",
        beats=[Beat(NAME, first.id), Beat(other, second.record.id)],
        lease=store.lease,
    )

    # Assert
    assert [(r.name, r.job_id, r.held) for r in replies] == [
        (NAME, first.id, True),
        (other, second.record.id, True),
    ]


async def test_job_backend_claim_takes_only_the_names_given(
    store: _Store,
) -> None:
    """A runner never claims a job of a name it does not run."""
    # Arrange
    await store.submit(name="app:other")

    # Act
    claimed = await _claim(store)

    # Assert
    assert claimed == []


async def test_job_backend_submit_with_a_free_slot_starts_the_job(
    store: _Store,
) -> None:
    """`start_as` runs the job at once, held by that runner."""
    # Arrange
    arguments = store.args(start_as="r")

    # Act
    submitted = await store.backend.submit(**arguments)

    # Assert
    assert submitted.started is True
    assert submitted.record is not None
    assert submitted.record.status is JobStatus.RUNNING
    assert submitted.record.started_at is not None
    assert await _claim(store) == []


async def test_job_backend_submit_never_jumps_the_queue(store: _Store) -> None:
    """A free slot does not overtake a job already queued."""
    # Arrange
    await store.submit()

    # Arrange
    arguments = store.args(start_as="r")

    # Act
    submitted = await store.backend.submit(**arguments)

    # Assert
    assert submitted.started is False
    assert submitted.record is not None
    assert submitted.record.status is JobStatus.QUEUED


@pytest.mark.parametrize(
    ("max_queued", "accepted"),
    [
        pytest.param(None, 3, id="no-limit"),
        pytest.param(2, 2, id="limit"),
        pytest.param(0, 0, id="zero-refuses-a-queued-job"),
    ],
)
async def test_job_backend_submit_refuses_past_max_queued(
    store: _Store, max_queued: int | None, accepted: int
) -> None:
    """Past `max_queued` queued jobs of a name, `submit` stores nothing."""
    # Arrange
    arguments = [store.args(max_queued=max_queued) for _ in range(3)]

    # Act
    submitted = [await store.backend.submit(**a) for a in arguments]

    # Assert
    assert sum(s.record is not None for s in submitted) == accepted
    assert len(await _claim(store)) == accepted


async def test_job_backend_max_queued_counts_one_name_only(
    store: _Store,
) -> None:
    """A full queue of one name does not refuse another name."""
    # Arrange
    await store.submit(max_queued=1)

    # Act
    other = await store.backend.submit(
        **store.args(name="app:other", max_queued=1)
    )

    # Assert
    assert other.record is not None


async def test_job_backend_max_queued_zero_runs_with_a_free_slot(
    store: _Store,
) -> None:
    """`max_queued=0` with a free slot runs the job now."""
    # Arrange
    arguments = store.args(max_queued=0, start_as="r")

    # Act
    submitted = await store.backend.submit(**arguments)

    # Assert
    assert submitted.started is True


async def _running(store: _Store, runner: str = "r") -> JobRecord:
    """Store a job this runner holds, and return its record."""
    submitted = await store.backend.submit(**store.args(start_as=runner))
    assert submitted.record is not None
    return submitted.record


async def _read(store: _Store, job_id: str) -> JobRecord:
    """Return a job that must still be readable."""
    record = await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)
    assert record is not None
    return record


async def _settle(
    store: _Store,
    job_id: str,
    runner: str = "r",
    status: EndStatus = JobStatus.SUCCEEDED,
    error: str | None = None,
    result: str | None = None,
    progress: ProgressUpdate | None = None,
) -> bool:
    """End a job as `runner`, and return whether the store took it."""
    return await store.backend.settle(
        name=NAME,
        job_id=job_id,
        runner=runner,
        status=status,
        error=error,
        result=result,
        progress=progress,
    )


async def test_job_backend_beat_writes_progress_and_bumps_the_version(
    store: _Store,
) -> None:
    """A beat with progress shows it to readers, at a new version."""
    # Arrange
    record = await _running(store)

    # Act
    [reply] = await store.backend.beat(
        runner="r",
        beats=[Beat(NAME, record.id, ProgressUpdate(done=2, total=5))],
        lease=store.lease,
    )
    read = await _read(store, record.id)

    # Assert
    assert (reply.job_id, reply.held) == (record.id, True)
    assert (read.done, read.total) == (2, 5)
    assert read.version == record.version + 1


async def test_job_backend_beat_alone_keeps_the_version(store: _Store) -> None:
    """Extending a lease shows nothing new, so the version stays."""
    # Arrange
    record = await _running(store)

    # Act
    await store.backend.beat(
        runner="r", beats=[Beat(NAME, record.id)], lease=store.lease
    )

    # Assert
    assert (await _read(store, record.id)).version == record.version


async def test_job_backend_beat_from_another_runner_writes_nothing(
    store: _Store,
) -> None:
    """A runner that does not hold the job is told so, and changes nothing."""
    # Arrange
    record = await _running(store)

    # Act
    [reply] = await store.backend.beat(
        runner="stale",
        beats=[Beat(NAME, record.id, ProgressUpdate(done=9, total=9))],
        lease=store.lease,
    )

    # Assert
    assert reply.held is False
    assert (await _read(store, record.id)).done == 0


async def test_job_backend_beat_handles_each_job_on_its_own(
    store: _Store,
) -> None:
    """One beat covers many jobs, in order, and an unknown id is not held."""
    # Arrange
    record = await _running(store)

    # Act
    replies = await store.backend.beat(
        runner="r",
        beats=[Beat(NAME, "nope"), Beat(NAME, record.id)],
        lease=store.lease,
    )

    # Assert
    assert [(r.job_id, r.held) for r in replies] == [
        ("nope", False),
        (record.id, True),
    ]


async def test_job_backend_settle_ends_the_job_with_its_result(
    store: _Store,
) -> None:
    """Settling writes the end state, the result and the last counts."""
    # Arrange
    record = await _running(store)

    # Act
    settled = await _settle(
        store,
        record.id,
        result='{"imported": 3}',
        progress=ProgressUpdate(done=3, total=3),
    )
    read = await _read(store, record.id)

    # Assert
    assert settled is True
    assert read.status is JobStatus.SUCCEEDED
    assert read.result == '{"imported": 3}'
    assert (read.done, read.total) == (3, 3)
    assert read.finished_at is not None
    assert read.version > record.version


async def test_job_backend_settle_keeps_the_error(store: _Store) -> None:
    """A failed job reads back with its error."""
    # Arrange
    record = await _running(store)

    # Act
    await _settle(store, record.id, status=JobStatus.FAILED, error="Row 3")

    # Assert
    read = await _read(store, record.id)
    assert (read.status, read.error) == (JobStatus.FAILED, "Row 3")


async def test_job_backend_settle_from_another_runner_writes_nothing(
    store: _Store,
) -> None:
    """A runner that does not hold the job cannot end it."""
    # Arrange
    record = await _running(store)

    # Act
    settled = await _settle(store, record.id, runner="stale")

    # Assert
    assert settled is False
    assert (await _read(store, record.id)).status is JobStatus.RUNNING


async def test_job_backend_settle_retry_writes_nothing_more(
    store: _Store,
) -> None:
    """A retry after a lost reply is taken, and the job stays as it ended."""
    # Arrange
    record = await _running(store)
    await _settle(store, record.id, result='"first"')
    ended = await _read(store, record.id)

    # Act
    retried = await _settle(store, record.id, result='"first"')

    # Assert
    assert retried is True
    assert await _read(store, record.id) == ended


async def test_job_backend_settle_never_leaves_an_end_state(
    store: _Store,
) -> None:
    """A job that ended stays ended, whatever a later settle says."""
    # Arrange
    record = await _running(store)
    await _settle(store, record.id, status=JobStatus.CANCELLED)

    # Act
    settled = await _settle(store, record.id, status=JobStatus.SUCCEEDED)

    # Assert
    assert settled is False
    assert (await _read(store, record.id)).status is JobStatus.CANCELLED


async def test_job_backend_cancel_ends_a_queued_job(store: _Store) -> None:
    """A queued job ends `cancelled`, and no runner can claim it."""
    # Arrange
    record = await store.submit()

    # Act
    cancelled = await store.backend.cancel(
        name=NAME, job_id=record.id, owner=OWNER
    )

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.finished_at is not None
    assert await _claim(store) == []


async def test_job_backend_cancel_flags_a_running_job(store: _Store) -> None:
    """A running job keeps running, and its runner sees the flag on a beat."""
    # Arrange
    record = await _running(store)

    # Act
    cancelled = await store.backend.cancel(
        name=NAME, job_id=record.id, owner=OWNER
    )
    [reply] = await store.backend.beat(
        runner="r", beats=[Beat(NAME, record.id)], lease=store.lease
    )

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.RUNNING
    assert cancelled.cancel_requested is True
    assert cancelled.version == record.version + 1
    assert (reply.held, reply.cancel_requested) == (True, True)


async def test_job_backend_cancel_leaves_a_runner_able_to_settle(
    store: _Store,
) -> None:
    """A cancel bumps the version, and never fences the runner out."""
    # Arrange
    record = await _running(store)
    await store.backend.cancel(name=NAME, job_id=record.id, owner=OWNER)

    # Act
    settled = await _settle(store, record.id, status=JobStatus.CANCELLED)

    # Assert
    assert settled is True


async def test_job_backend_cancel_changes_nothing_on_an_ended_job(
    store: _Store,
) -> None:
    """Cancelling a finished job returns it as it is."""
    # Arrange
    record = await _running(store)
    await _settle(store, record.id)
    ended = await _read(store, record.id)

    # Act
    cancelled = await store.backend.cancel(
        name=NAME, job_id=record.id, owner=OWNER
    )

    # Assert
    assert cancelled == ended


async def test_job_backend_cancel_twice_bumps_the_version_once(
    store: _Store,
) -> None:
    """A second cancel of a running job changes nothing a reader can see."""
    # Arrange
    record = await _running(store)
    first = await store.backend.cancel(name=NAME, job_id=record.id, owner=OWNER)

    # Act
    second = await store.backend.cancel(
        name=NAME, job_id=record.id, owner=OWNER
    )

    # Assert
    assert second == first


async def test_job_backend_cancel_reaches_a_job_only_for_its_owner(
    store: _Store,
) -> None:
    """Another owner gets `None`, and the job does not change."""
    # Arrange
    record = await store.submit()

    # Act
    cancelled = await store.backend.cancel(
        name=NAME, job_id=record.id, owner="bob"
    )

    # Assert
    assert cancelled is None
    assert (await _read(store, record.id)).status is JobStatus.QUEUED


async def test_job_backend_reads_a_lapsed_job_as_failed(store: _Store) -> None:
    """A job whose lease lapsed reads `failed`, without any reaper."""
    # Arrange
    record = await _running(store)

    # Act
    await store.elapse(LEASE_TICKS + MARGIN)
    read = await _read(store, record.id)

    # Assert
    assert read.status is JobStatus.FAILED
    assert read.error == JOB_LOST
    assert read.finished_at is not None
    assert read.version == record.version + 1


async def test_job_backend_beat_keeps_a_job_alive(store: _Store) -> None:
    """Each beat pushes the lease out from now."""
    # Arrange
    record = await _running(store)
    await store.elapse(LEASE_TICKS - MARGIN)

    # Act
    await store.backend.beat(
        runner="r", beats=[Beat(NAME, record.id)], lease=store.lease
    )
    await store.elapse(LEASE_TICKS - MARGIN)

    # Assert
    assert (await _read(store, record.id)).status is JobStatus.RUNNING


async def test_job_backend_lapsed_runner_is_fenced_out(store: _Store) -> None:
    """After a lapse, the runner's late writes change nothing."""
    # Arrange
    record = await _running(store)
    await store.elapse(LEASE_TICKS + MARGIN)
    lost = await _read(store, record.id)

    # Act
    [reply] = await store.backend.beat(
        runner="r",
        beats=[Beat(NAME, record.id, ProgressUpdate(done=1, total=1))],
        lease=store.lease,
    )
    settled = await _settle(store, record.id, status=JobStatus.FAILED)

    # Assert
    assert reply.held is False
    assert settled is False
    assert await _read(store, record.id) == lost


async def test_job_backend_beat_past_the_lease_cannot_revive_a_job(
    store: _Store,
) -> None:
    """A beat that arrives after the lease ended does not extend it."""
    # Arrange
    record = await _running(store)
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    [reply] = await store.backend.beat(
        runner="r", beats=[Beat(NAME, record.id)], lease=store.lease
    )

    # Assert
    assert reply.held is False
    assert (await _read(store, record.id)).status is JobStatus.FAILED


async def test_job_backend_settle_past_the_lease_is_refused(
    store: _Store,
) -> None:
    """A settle that arrives after the lease ended loses to the lapse."""
    # Arrange
    record = await _running(store)
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    settled = await _settle(store, record.id)

    # Assert
    assert settled is False
    assert (await _read(store, record.id)).error == JOB_LOST


async def test_job_backend_reap_ends_lapsed_jobs_of_any_name(
    store: _Store,
) -> None:
    """`reap` ends every lapsed job, including a name nobody runs now."""
    # Arrange
    await store.backend.submit(**store.args(name="app:gone", start_as="r"))
    live = await _running(store)
    await store.elapse(LEASE_TICKS - MARGIN)
    await store.backend.beat(
        runner="r", beats=[Beat(NAME, live.id)], lease=store.lease
    )
    await store.elapse(2 * MARGIN)

    # Act
    reaped = await store.backend.reap(limit=10)

    # Assert
    assert reaped == 1
    assert (await _read(store, live.id)).status is JobStatus.RUNNING


async def test_job_backend_reap_respects_its_limit(store: _Store) -> None:
    """`reap` ends at most `limit` jobs, and the next call ends the rest."""
    # Arrange
    await _running(store, "r1")
    await _running(store, "r2")
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    first = await store.backend.reap(limit=1)
    second = await store.backend.reap(limit=10)

    # Assert
    assert (first, second) == (1, 1)


async def test_job_backend_reads_a_job_past_max_wait_as_failed(
    store: _Store,
) -> None:
    """A queued job past `max_wait` ends `failed`, and is never claimed."""
    # Arrange
    record = await store.submit(max_wait=store.ticks(60))

    # Act
    await store.elapse(60 + MARGIN)
    read = await _read(store, record.id)

    # Assert
    assert read.status is JobStatus.FAILED
    assert read.error == JOB_NOT_STARTED
    assert await _claim(store) == []


async def test_job_backend_claim_skips_a_job_past_max_wait(
    store: _Store,
) -> None:
    """A claim ends an expired job instead of running it."""
    # Arrange
    expired = await store.submit(max_wait=store.ticks(60))
    fresh = await store.submit(max_wait=None)
    await store.elapse(60 + MARGIN)

    # Act
    claimed = await _claim(store)

    # Assert
    assert claimed == [fresh.id]
    assert (await _read(store, expired.id)).status is JobStatus.FAILED


async def test_job_backend_max_wait_none_never_expires(store: _Store) -> None:
    """With no `max_wait`, a queued job waits as long as it takes."""
    # Arrange
    record = await store.submit(max_wait=None)

    # Act
    await store.elapse(200)

    # Assert
    assert (await _read(store, record.id)).status is JobStatus.QUEUED


async def test_job_backend_reap_ends_jobs_past_max_wait_of_any_name(
    store: _Store,
) -> None:
    """A job queued under a name nobody runs still ends."""
    # Arrange
    await store.submit(name="app:gone", max_wait=store.ticks(60))
    await store.elapse(60 + MARGIN)

    # Act
    reaped = await store.backend.reap(limit=10)

    # Assert
    assert reaped == 1


async def test_job_backend_expired_jobs_leave_the_queue_count(
    store: _Store,
) -> None:
    """A job past `max_wait` no longer takes a place under `max_queued`."""
    # Arrange
    await store.submit(max_queued=1, max_wait=store.ticks(60))
    await store.elapse(60 + MARGIN)

    # Act
    submitted = await store.backend.submit(**store.args(max_queued=1))

    # Assert
    assert submitted.record is not None


@pytest.mark.parametrize(
    ("keep", "readable_for"),
    [
        pytest.param(60, 60, id="ticks"),
        pytest.param(0, 0, id="zero-keeps-nothing"),
    ],
)
async def test_job_backend_get_forgets_a_job_past_keep(
    store: _Store, keep: int, readable_for: int
) -> None:
    """An ended job reads as not found once its retention ends."""
    # Arrange
    submitted = await store.backend.submit(
        **store.args(start_as="r", keep=store.ticks(keep))
    )
    assert submitted.record is not None
    job_id = submitted.record.id
    await _settle(store, job_id)

    # Act
    if readable_for:
        await store.elapse(readable_for - MARGIN)
        before = await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)
        assert before is not None
        await store.elapse(2 * MARGIN)
    after = await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)

    # Assert
    assert after is None


async def test_job_backend_keep_none_keeps_an_ended_job(store: _Store) -> None:
    """With no `keep`, an ended job stays readable."""
    # Arrange
    submitted = await store.backend.submit(
        **store.args(start_as="r", keep=None)
    )
    assert submitted.record is not None
    await _settle(store, submitted.record.id)

    # Act
    await store.elapse(200)

    # Assert
    assert (await _read(store, submitted.record.id)) is not None


async def test_job_backend_keep_starts_at_each_end_state(store: _Store) -> None:
    """Retention also runs for a job ended by a cancel, lapse or `max_wait`."""
    # Arrange
    keep = store.ticks(60)
    cancelled = await store.submit(keep=keep)
    await store.backend.cancel(name=NAME, job_id=cancelled.id, owner=OWNER)
    expired = await store.submit(keep=keep, max_wait=store.ticks(10))
    await store.elapse(10 + MARGIN)
    await store.backend.reap(limit=10)

    # Act
    await store.elapse(60 + MARGIN)

    # Assert
    for job_id in (cancelled.id, expired.id):
        read = await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)
        assert read is None


async def test_job_backend_purge_forgets_ended_jobs_of_any_name(
    store: _Store,
) -> None:
    """After `purge`, a job past retention is gone, whatever its name."""
    # Arrange
    job_ids = {}
    for name in (NAME, "app:gone"):
        submitted = await store.backend.submit(
            **store.args(name=name, start_as="r", keep=store.ticks(60))
        )
        assert submitted.record is not None
        job_ids[name] = submitted.record.id
        await store.backend.settle(
            name=name,
            job_id=submitted.record.id,
            runner="r",
            status=JobStatus.SUCCEEDED,
            error=None,
            result=None,
            progress=None,
        )
    await store.elapse(60 + MARGIN)

    # Act
    await store.backend.purge(limit=10)

    # Assert
    for name, job_id in job_ids.items():
        assert (
            await store.backend.get(name=name, job_id=job_id, owner=OWNER)
        ) is None


async def test_job_backend_submit_retried_after_a_lost_reply_still_starts(
    store: _Store,
) -> None:
    """The same runner retrying a started job is told to run it."""
    # Arrange
    arguments = store.args(start_as="r")
    first = await store.backend.submit(**arguments)

    # Act
    second = await store.backend.submit(**arguments)

    # Assert
    assert (first.started, second.started) == (True, True)
    assert second.record == first.record


async def test_job_backend_submit_retried_by_another_runner_does_not_start(
    store: _Store,
) -> None:
    """Only the runner that holds the job is told to run it."""
    # Arrange
    arguments = store.args(start_as="r")
    first = await store.backend.submit(**arguments)
    retry: dict[str, Any] = {**arguments, "start_as": "other"}

    # Act
    second = await store.backend.submit(**retry)

    # Assert
    assert second.started is False
    assert second.record == first.record


async def test_job_backend_submit_retried_keeps_the_read_rules(
    store: _Store,
) -> None:
    """A retry never shows a job as `running` past its lease."""
    # Arrange
    arguments = store.args(start_as="r")
    await store.backend.submit(**arguments)
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    retried = await store.backend.submit(**arguments)

    # Assert
    assert retried.started is False
    assert retried.record is not None
    assert retried.record.status is JobStatus.FAILED
    assert retried.record.error == JOB_LOST


async def test_job_backend_submit_retried_after_a_claim_does_not_start(
    store: _Store,
) -> None:
    """A job the runner claimed later is already running, so a retry never starts it."""
    # Arrange
    arguments = store.args(start_as="r")
    blocker = await store.submit()
    queued = await store.backend.submit(**arguments)
    assert queued.started is False
    assert await _claim(store, "r", limit=2) == [
        blocker.id,
        arguments["job_id"],
    ]

    # Act
    retried = await store.backend.submit(**arguments)

    # Assert
    assert retried.started is False
    assert retried.record is not None
    assert retried.record.status is JobStatus.RUNNING


async def test_job_backend_id_past_retention_is_free_for_another_name(
    store: _Store,
) -> None:
    """A job past retention takes no id, whoever asks and whether or not purged."""
    # Arrange
    arguments = store.args(start_as="r", keep=store.ticks(10))
    await store.backend.submit(**arguments)
    await _settle(store, arguments["job_id"])
    await store.elapse(10 + MARGIN)
    other: dict[str, Any] = {**arguments, "name": "app:other", "start_as": None}

    # Act
    submitted = await store.backend.submit(**other)

    # Assert
    assert submitted.record is not None
    assert submitted.record.name == "app:other"


@pytest.mark.parametrize(
    "other",
    [
        pytest.param({"owner": "bob"}, id="another-owner"),
        pytest.param({"owner": None}, id="no-owner"),
        pytest.param({"name": "app:other"}, id="another-name"),
    ],
)
async def test_job_backend_submit_with_a_taken_id_hands_nothing_over(
    store: _Store, other: dict[str, Any]
) -> None:
    """Another owner or name reusing an id gets nothing, and changes nothing."""
    # Arrange
    arguments = store.args(start_as="r")
    await store.backend.submit(**arguments)
    await store.backend.settle(
        name=NAME,
        job_id=arguments["job_id"],
        runner="r",
        status=JobStatus.SUCCEEDED,
        error=None,
        result='"secret"',
        progress=None,
    )
    ended = await _read(store, arguments["job_id"])

    # Act
    retried = await store.backend.submit(**{**arguments, **other})

    # Assert
    assert retried.record is None
    assert retried.started is False
    assert await _read(store, arguments["job_id"]) == ended


async def test_job_backend_context_is_kept_as_submitted(store: _Store) -> None:
    """Changing the context after `submit` does not change what a claim gets."""
    # Arrange
    context = {"trace": "first"}
    await store.submit(context=context)
    context["trace"] = "changed"

    # Act
    [claimed] = await store.backend.claim(
        slots={NAME: 1}, runner="r", lease=store.lease
    )

    # Assert
    assert claimed.context == {"trace": "first"}


async def test_job_backend_beat_with_the_same_progress_writes_it_once(
    store: _Store,
) -> None:
    """A retried beat after a lost reply shows no new version."""
    # Arrange
    record = await _running(store)
    beats = [Beat(NAME, record.id, ProgressUpdate(done=2, total=5))]
    await store.backend.beat(runner="r", beats=beats, lease=store.lease)
    first = await _read(store, record.id)

    # Act
    await store.backend.beat(runner="r", beats=beats, lease=store.lease)

    # Assert
    assert await _read(store, record.id) == first


async def test_job_backend_settle_retried_with_keep_zero_is_refused(
    store: _Store,
) -> None:
    """With nothing kept, a retry finds no job."""
    # Arrange
    submitted = await store.backend.submit(
        **store.args(start_as="r", keep=store.ticks(0))
    )
    assert submitted.record is not None
    await _settle(store, submitted.record.id)

    # Act
    retried = await _settle(store, submitted.record.id)

    # Assert
    assert retried is False


async def test_job_backend_cancel_keeps_the_read_rules(store: _Store) -> None:
    """A cancel sees a lapsed job as `failed`, and an expired one as gone."""
    # Arrange
    lapsed = await _running(store)
    expired = await store.backend.submit(
        **store.args(start_as="r2", keep=store.ticks(0))
    )
    assert expired.record is not None
    await _settle(store, expired.record.id, runner="r2")
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    cancelled = await store.backend.cancel(
        name=NAME, job_id=lapsed.id, owner=OWNER
    )
    gone = await store.backend.cancel(
        name=NAME, job_id=expired.record.id, owner=OWNER
    )

    # Assert
    assert cancelled is not None
    assert cancelled.status is JobStatus.FAILED
    assert cancelled.cancel_requested is False
    assert gone is None


async def test_job_backend_writes_under_another_name_find_nothing(
    store: _Store,
) -> None:
    """The job name is part of the key for every write."""
    # Arrange
    record = await _running(store)

    # Act
    [reply] = await store.backend.beat(
        runner="r", beats=[Beat("app:other", record.id)], lease=store.lease
    )
    settled = await store.backend.settle(
        name="app:other",
        job_id=record.id,
        runner="r",
        status=JobStatus.SUCCEEDED,
        error=None,
        result=None,
        progress=None,
    )
    cancelled = await store.backend.cancel(
        name="app:other", job_id=record.id, owner=OWNER
    )

    # Assert
    assert reply.held is False
    assert settled is False
    assert cancelled is None
    assert await _read(store, record.id) == record


async def test_job_backend_max_queued_holds_under_concurrent_submits(
    store: _Store,
) -> None:
    """Many submits at once never pass `max_queued`."""
    # Arrange
    arguments = [store.args(max_queued=3) for _ in range(10)]

    # Act
    submitted = await asyncio.gather(
        *(store.backend.submit(**a) for a in arguments)
    )

    # Assert
    assert sum(s.record is not None for s in submitted) == 3  # noqa: PLR2004
    assert len(await _claim(store)) == 3  # noqa: PLR2004


async def test_job_backend_claim_empty_when_every_job_expired(
    store: _Store,
) -> None:
    """A queue of only expired jobs claims nothing."""
    # Arrange
    await store.submit(max_wait=store.ticks(10))
    await store.elapse(10 + MARGIN)

    # Act
    claimed = await _claim(store)

    # Assert
    assert claimed == []
