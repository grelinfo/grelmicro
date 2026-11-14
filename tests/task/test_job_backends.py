"""Test the contract every job backend keeps."""

import asyncio
import secrets
import time
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
    JOB_TIMED_OUT,
    Beat,
    BeatReply,
    EndStatus,
    JobBackend,
    JobRecord,
    Listed,
    ProgressUpdate,
    Settled,
    scan_limit,
)
from grelmicro.task.memory import MemoryJobAdapter
from grelmicro.task.postgres import PostgresJobAdapter
from grelmicro.task.redis import RedisJobAdapter
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
"""Ticks kept clear of every deadline, so no step lands on one exactly."""


@dataclass
class _Store:
    """A backend under test, and how to move its time forward.

    Time is counted in ticks. Each store reads a virtual clock that moves
    only when a test elapses ticks.
    """

    backend: JobBackend
    advance: Callable[[float], Awaitable[object]]
    tick: timedelta = timedelta(seconds=1)
    twin: JobBackend | None = None
    """Another replica's client to the same store, when not `backend`."""

    @property
    def replicas(self) -> tuple[JobBackend, JobBackend]:
        """Return two clients to the store, as two replicas hold them."""
        return self.backend, self.twin or self.backend

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
            "max_run": None,
            "keep": timedelta(hours=1),
            "max_kept_succeeded": None,
            "max_kept_failed": None,
            "start_as": None,
            "lease": self.lease,
            **overrides,
        }

    async def submit(self, **overrides: Any) -> JobRecord:  # noqa: ANN401
        """Store a queued job, and return its record."""
        submitted = await self.backend.submit(**self.args(**overrides))
        assert submitted.record is not None
        return submitted.record


REDIS_TICK = timedelta(minutes=1)
"""A tick on Redis, far longer than the real time a test takes.

Redis drops a key past `keep` on its own clock, so a key expiry keeps
running in real time between two steps.
"""

_REDIS_TIME = """
    local now_pair = redis.call('TIME')
    local now = tonumber(now_pair[1]) * 1000000 + tonumber(now_pair[2])
"""
"""How each job script reads the Redis server clock."""

_REDIS_VIRTUAL_TIME = """
    local now = tonumber(redis.call(
        'GET', string.sub(KEYS[1], 1, #KEYS[1] - #'deadlines') .. 'clock'
    ))
"""
"""How each job script reads the virtual clock of its store instead.

The clock key sits under the store's prefix, so it shares the slot of the
store's other keys on a Redis Cluster.
"""


def _redis_virtual_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every Redis job script read its store's virtual clock."""
    for name, script in vars(RedisJobAdapter).items():
        if name.startswith("_LUA_") and _REDIS_TIME in script:
            monkeypatch.setattr(
                RedisJobAdapter,
                name,
                script.replace(_REDIS_TIME, _REDIS_VIRTUAL_TIME),
            )
    assert _REDIS_TIME not in RedisJobAdapter._LUA_BEAT


async def _redis_clock(adapter: RedisJobAdapter) -> Callable[[float], Any]:
    """Start the virtual clock of a Redis job store at the real time.

    Return how to move it forward. A move also brings each key expiry of
    the store forward by the same time, and deletes a key it brings due.
    """
    client = adapter.provider.client
    base = adapter._base
    clock = f"{base}clock"
    await client.set(clock, time.time_ns() // 1000)

    async def advance(seconds: float) -> None:
        step = round(seconds * 1000)
        await client.incrby(clock, step * 1000)
        async for key in client.scan_iter(match=f"{base}*"):
            remaining = await client.pttl(key)
            if remaining < 0:
                continue
            if remaining <= step:
                await client.delete(key)
            else:
                await client.pexpire(key, remaining - step)

    return advance


async def _postgres_clock(
    postgres: PostgresProvider, *adapters: PostgresJobAdapter
) -> tuple[str, Callable[[float], Any]]:
    """Make Postgres job stores read a virtual clock, set at the real time.

    Return the clock's table and how to move the clock forward.
    """
    table = f"clock_{secrets.token_hex(6)}"
    await postgres.client.execute(
        f"CREATE TABLE {table} AS SELECT clock_timestamp() AS at"
    )
    for adapter in adapters:
        adapter._sql["now"] = f"SELECT at FROM {table};"  # noqa: S608

    async def advance(seconds: float) -> None:
        await postgres.client.execute(
            f"UPDATE {table} SET at = at + make_interval(secs => $1)",  # noqa: S608
            seconds,
        )

    return table, advance


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
        _redis_virtual_clock(request.getfixturevalue("monkeypatch"))
        prefix = hash_tag_prefix()
        adapter = provider.job_backend(prefix=prefix)
        twin = provider.job_backend(prefix=prefix)
        async with provider, adapter, twin:
            advance = await _redis_clock(adapter)
            yield _Store(adapter, advance, REDIS_TICK, twin)
        return
    if request.param == "postgres":
        postgres = PostgresProvider(request.getfixturevalue("postgres_url"))
        table_name = f"jobs_{secrets.token_hex(6)}"
        async with (
            postgres,
            postgres.job_backend(table_name=table_name) as pg,
            postgres.job_backend(table_name=table_name) as pg_twin,
        ):
            clock_table, advance = await _postgres_clock(postgres, pg, pg_twin)
            yield _Store(pg, advance, twin=pg_twin)
            await postgres.client.execute(f"DROP TABLE IF EXISTS {table_name}")
            await postgres.client.execute(f"DROP TABLE IF EXISTS {clock_table}")
        return
    if request.param == "sqlite":
        path = request.getfixturevalue("tmp_path") / "jobs.db"
        sqlite = SQLiteProvider(path)
        other = SQLiteProvider(path)
        clock = VirtualClock()
        started = round(time.time() * 1000)

        async def _now(_conn: object) -> int:
            return started + round(clock.monotonic() * 1000)

        request.getfixturevalue("monkeypatch").setattr(
            "grelmicro.task.sqlite._now", _now
        )
        async with (
            sqlite,
            other,
            sqlite.job_backend() as adapter,
            other.job_backend() as twin,
        ):
            yield _Store(adapter, clock.advance, twin=twin)
        return
    backend = MemoryJobAdapter()
    clock = VirtualClock()
    async with clock, backend:
        yield _Store(backend, clock.advance, twin=_memory_twin(backend))


def _memory_twin(backend: MemoryJobAdapter) -> MemoryJobAdapter:
    """Return another client to the jobs `backend` holds, as a replica's."""
    twin = MemoryJobAdapter()
    vars(twin).update(vars(backend))
    return twin


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
) -> Settled:
    """End a job as `runner`, and return what the store did."""
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
    assert settled == Settled(settled=True, error=None)
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
    assert settled == Settled(settled=False, error=None)
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
    assert retried == Settled(settled=True, error=None)
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
    assert settled == Settled(settled=False, error=None)
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
    assert settled == Settled(settled=True, error=None)


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
    assert settled == Settled(settled=False, error=JOB_LOST)
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
    assert settled == Settled(settled=False, error=JOB_LOST)
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


MAX_RUN_TICKS = 10
"""A job's `max_run` in the tests, in ticks, well inside its lease."""


async def _beat(store: _Store, job_id: str, runner: str = "r") -> BeatReply:
    """Beat one job of `NAME` as `runner`, and return the reply."""
    [reply] = await store.backend.beat(
        runner=runner, beats=[Beat(NAME, job_id)], lease=store.lease
    )
    return reply


@pytest.mark.parametrize("start", ["submit", "claim"])
async def test_job_backend_reads_a_job_past_max_run_as_failed(
    store: _Store, start: str
) -> None:
    """A running job past `max_run` ends `failed`, without any reaper."""
    # Arrange
    max_run = store.ticks(MAX_RUN_TICKS)
    if start == "submit":
        record = await store.submit(start_as="r", max_run=max_run)
    else:
        record = await store.submit(max_run=max_run)
        assert await _claim(store) == [record.id]

    # Act
    await store.elapse(MAX_RUN_TICKS + MARGIN)
    read = await _read(store, record.id)

    # Assert
    assert read.status is JobStatus.FAILED
    assert read.error == JOB_TIMED_OUT
    assert read.finished_at is not None
    assert await _settle(store, record.id) == Settled(
        settled=False, error=JOB_TIMED_OUT
    )


async def test_job_backend_settle_past_max_run_with_keep_zero_says_why(
    store: _Store,
) -> None:
    """A settle that ends a job past `max_run` says why, though none is kept."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS), keep=store.ticks(0)
    )
    await store.elapse(MAX_RUN_TICKS + MARGIN)

    # Act
    settled = await _settle(store, record.id)

    # Assert
    assert settled == Settled(settled=False, error=JOB_TIMED_OUT)
    gone = await store.backend.get(name=NAME, job_id=record.id, owner=OWNER)
    assert gone is None


async def test_job_backend_beat_past_max_run_lets_the_job_go(
    store: _Store,
) -> None:
    """A beat past `max_run` is not held, and says why the store ended it."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )
    await store.elapse(MAX_RUN_TICKS + MARGIN)

    # Act
    reply = await _beat(store, record.id)

    # Assert
    assert reply.held is False
    assert reply.error == JOB_TIMED_OUT
    assert (await _read(store, record.id)).error == JOB_TIMED_OUT


async def test_job_backend_beat_reports_an_end_a_read_made(
    store: _Store,
) -> None:
    """A beat after a read ended the job still says why it ended."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )
    await store.elapse(MAX_RUN_TICKS + MARGIN)
    await _read(store, record.id)

    # Act
    reply = await _beat(store, record.id)

    # Assert
    assert (reply.held, reply.error) == (False, JOB_TIMED_OUT)


async def test_job_backend_beat_past_the_lease_says_the_job_was_lost(
    store: _Store,
) -> None:
    """A lease that lapses before `max_run` ends the job lost."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(LEASE_TICKS + 4 * MARGIN)
    )
    await store.elapse(LEASE_TICKS + MARGIN)

    # Act
    reply = await _beat(store, record.id)

    # Assert
    assert (reply.held, reply.error) == (False, JOB_LOST)


async def test_job_backend_held_beat_has_no_error(store: _Store) -> None:
    """A beat on a held job carries no error."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )

    # Act
    reply = await _beat(store, record.id)

    # Assert
    assert (reply.held, reply.error) == (True, None)


async def test_job_backend_beat_never_pushes_past_max_run(
    store: _Store,
) -> None:
    """A job under `max_run` keeps running, and a beat does not extend it."""
    # Arrange
    max_run = LEASE_TICKS + 2 * MARGIN
    record = await store.submit(start_as="r", max_run=store.ticks(max_run))
    await store.elapse(LEASE_TICKS - MARGIN)
    await _beat(store, record.id)

    # Act
    await store.elapse(2 * MARGIN)
    under = await _read(store, record.id)
    await store.elapse(2 * MARGIN)
    reaped = await store.backend.reap(limit=10)
    past = await _read(store, record.id)

    # Assert
    assert under.status is JobStatus.RUNNING
    assert reaped == 1
    assert (past.status, past.error) == (JobStatus.FAILED, JOB_TIMED_OUT)


async def test_job_backend_reap_ends_jobs_past_max_run(store: _Store) -> None:
    """`reap` ends a job past `max_run` well before its lease lapses."""
    # Arrange
    record = await store.submit(
        name="app:gone", start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )
    await store.elapse(MAX_RUN_TICKS + MARGIN)

    # Act
    reaped = await store.backend.reap(limit=10)

    # Assert
    assert reaped == 1
    read = await store.backend.get(
        name="app:gone", job_id=record.id, owner=OWNER
    )
    assert read is not None
    assert read.error == JOB_TIMED_OUT


async def test_job_backend_max_run_none_never_expires(store: _Store) -> None:
    """With no `max_run`, a running job runs while its beats arrive."""
    # Arrange
    record = await _running(store)

    # Act
    for _ in range(3):
        await store.elapse(LEASE_TICKS - MARGIN)
        await _beat(store, record.id)

    # Assert
    assert (await _read(store, record.id)).status is JobStatus.RUNNING


async def test_job_backend_cancelled_job_past_max_run_ends_failed(
    store: _Store,
) -> None:
    """A cancel the body did not settle in time loses to `max_run`."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )
    await store.backend.cancel(name=NAME, job_id=record.id, owner=OWNER)

    # Act
    await store.elapse(MAX_RUN_TICKS + MARGIN)
    read = await _read(store, record.id)

    # Assert
    assert (read.status, read.error) == (JobStatus.FAILED, JOB_TIMED_OUT)
    assert read.cancel_requested is True
    assert await _settle(
        store, record.id, status=JobStatus.CANCELLED
    ) == Settled(settled=False, error=JOB_TIMED_OUT)


async def test_job_backend_cancel_settled_before_max_run_stays_cancelled(
    store: _Store,
) -> None:
    """A cancel the body settles before `max_run` ends the job `cancelled`."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )
    await store.backend.cancel(name=NAME, job_id=record.id, owner=OWNER)
    settled = await _settle(store, record.id, status=JobStatus.CANCELLED)

    # Act
    await store.elapse(MAX_RUN_TICKS + MARGIN)
    read = await _read(store, record.id)

    # Assert
    assert settled == Settled(settled=True, error=None)
    assert (read.status, read.error) == (JobStatus.CANCELLED, None)


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
    assert retried == Settled(settled=False, error=None)


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
    assert settled == Settled(settled=False, error=None)
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


async def _list(
    store: _Store,
    owner: str = OWNER,
    status: JobStatus | None = None,
    limit: int = 25,
    after: str | None = None,
) -> Listed:
    """List the jobs of `NAME` that `owner` owns."""
    return await store.backend.list(
        name=NAME, owner=owner, status=status, limit=limit, after=after
    )


def _ids(listed: Listed) -> list[str]:
    """Return the ids a list returned, in its order."""
    return [record.id for record in listed.records]


async def test_job_backend_list_returns_the_newest_first(store: _Store) -> None:
    """An owner's jobs list in reverse submission order, on one page."""
    # Arrange
    jobs = [await store.submit() for _ in range(3)]

    # Act
    listed = await _list(store)

    # Assert
    assert listed.records == list(reversed(jobs))
    assert listed.next is None


async def test_job_backend_list_with_no_job_is_empty(store: _Store) -> None:
    """An owner with no job lists nothing, on a last page."""
    # Act
    listed = await _list(store)

    # Assert
    assert listed == Listed(records=[], next=None)


async def test_job_backend_list_keeps_only_the_status_asked(
    store: _Store,
) -> None:
    """`status` keeps the jobs in that status, and `None` keeps every one."""
    # Arrange
    ended = await _running(store)
    await _settle(store, ended.id)
    running = await _running(store)
    queued = await store.submit()

    # Act
    only_running = await _list(store, status=JobStatus.RUNNING)
    only_queued = await _list(store, status=JobStatus.QUEUED)
    only_ended = await _list(store, status=JobStatus.SUCCEEDED)
    none_failed = await _list(store, status=JobStatus.FAILED)
    every = await _list(store)

    # Assert
    assert _ids(only_running) == [running.id]
    assert _ids(only_queued) == [queued.id]
    assert _ids(only_ended) == [ended.id]
    assert none_failed == Listed(records=[], next=None)
    assert _ids(every) == [queued.id, running.id, ended.id]


@pytest.mark.parametrize("count", [5, 4], ids=["partial-last", "full-last"])
async def test_job_backend_list_pages_until_next_is_none(
    store: _Store, count: int
) -> None:
    """Each page holds at most `limit` jobs, and `next` leads to the rest."""
    # Arrange
    jobs = [await store.submit() for _ in range(count)]
    newest_first = [job.id for job in reversed(jobs)]

    # Act
    pages: list[list[str]] = []
    after: str | None = None
    while True:
        listed = await _list(store, limit=2, after=after)
        pages.append(_ids(listed))
        if listed.next is None:
            break
        after = listed.next

    # Assert
    assert [job_id for page in pages for job_id in page] == newest_first
    assert all(len(page) == 2 for page in pages[:-1])  # noqa: PLR2004
    assert len(pages) == (count + 1) // 2
    assert pages[-1]


async def test_job_backend_list_next_page_ignores_newer_jobs(
    store: _Store,
) -> None:
    """A job submitted between two pages neither repeats nor skips one."""
    # Arrange
    jobs = [await store.submit() for _ in range(4)]
    first = await _list(store, limit=2)

    # Act
    newer = await store.submit()
    second = await _list(store, limit=2, after=first.next)
    fresh = await _list(store, limit=1)

    # Assert
    assert _ids(first) == [jobs[3].id, jobs[2].id]
    assert _ids(second) == [jobs[1].id, jobs[0].id]
    assert second.next is None
    assert _ids(fresh) == [newer.id]


async def test_job_backend_list_shows_only_the_owner_s_jobs(
    store: _Store,
) -> None:
    """Jobs of another owner, of no owner, or of another name never list."""
    # Arrange
    mine = await store.submit()
    await store.submit(owner="bob")
    await store.submit(owner=None)
    await store.submit(owner="")
    await store.submit(name="app:other")

    # Act
    listed = await _list(store)
    bob = await _list(store, owner="bob")
    empty = await _list(store, owner="")

    # Assert
    assert _ids(listed) == [mine.id]
    assert len(bob.records) == 1
    assert bob.records[0].id != mine.id
    assert len(empty.records) == 1
    assert empty.records[0].id not in {mine.id, bob.records[0].id}


async def test_job_backend_list_skips_a_job_past_keep(store: _Store) -> None:
    """A forgotten job is not listed, and a cursor naming it still pages on."""
    # Arrange
    oldest = await _running(store)
    gone = await store.submit(start_as="r", keep=store.ticks(10))
    newest = await _running(store)
    first = await _list(store, limit=2)
    assert _ids(first) == [newest.id, gone.id]
    await _settle(store, gone.id)

    # Act
    await store.elapse(10 + MARGIN)
    second = await _list(store, limit=2, after=first.next)
    every = await _list(store)

    # Assert
    assert _ids(second) == [oldest.id]
    assert second.next is None
    assert _ids(every) == [newest.id, oldest.id]


async def test_job_backend_list_skips_a_job_kept_for_zero(
    store: _Store,
) -> None:
    """A job that ends with `keep` at zero is gone from the list at once."""
    # Arrange
    kept = await store.submit()
    gone = await store.submit(start_as="r", keep=timedelta(0))
    assert gone.status is JobStatus.QUEUED
    await store.backend.cancel(name=NAME, job_id=gone.id, owner=OWNER)

    # Act
    listed = await _list(store)

    # Assert
    assert _ids(listed) == [kept.id]


async def test_job_backend_list_shows_a_lapsed_job_as_failed(
    store: _Store,
) -> None:
    """A job past its lease lists `failed`, as a read moves it."""
    # Arrange
    record = await _running(store)

    # Act
    await store.elapse(LEASE_TICKS + MARGIN)
    listed = await _list(store)
    running = await _list(store, status=JobStatus.RUNNING)

    # Assert
    [read] = listed.records
    assert read.status is JobStatus.FAILED
    assert read.error == JOB_LOST
    assert read.version == record.version + 1
    assert running.records == []
    assert await _read(store, record.id) == read
    assert await _settle(store, record.id) == Settled(
        settled=False, error=JOB_LOST
    )


async def test_job_backend_list_shows_a_job_past_max_wait_as_failed(
    store: _Store,
) -> None:
    """A queued job past its `max_wait` lists `failed`, out of the queue."""
    # Arrange
    record = await store.submit(max_wait=store.ticks(10))

    # Act
    await store.elapse(10 + MARGIN)
    listed = await _list(store, status=JobStatus.FAILED)

    # Assert
    [read] = listed.records
    assert read.id == record.id
    assert read.error == JOB_NOT_STARTED
    assert await _claim(store) == []


async def test_job_backend_list_shows_a_job_past_max_run_as_failed(
    store: _Store,
) -> None:
    """A running job past its `max_run` lists `failed`."""
    # Arrange
    record = await store.submit(
        start_as="r", max_run=store.ticks(MAX_RUN_TICKS)
    )

    # Act
    await store.elapse(MAX_RUN_TICKS + MARGIN)
    listed = await _list(store)

    # Assert
    [read] = listed.records
    assert read.id == record.id
    assert read.status is JobStatus.FAILED
    assert read.error == JOB_TIMED_OUT


async def test_job_backend_list_with_a_lapsed_keep_zero_job_lists_nothing(
    store: _Store,
) -> None:
    """A job a list ends with `keep` at zero is gone at once."""
    # Arrange
    await store.submit(start_as="r", keep=timedelta(0))

    # Act
    await store.elapse(LEASE_TICKS + MARGIN)
    listed = await _list(store)

    # Assert
    assert listed == Listed(records=[], next=None)


@pytest.mark.parametrize(
    "after",
    [
        "",
        "not a cursor",
        "!!!!",
        "LTE",
        "MA",
        "eA",
        "MQ=",
        "OTk5OTk5OTk5OTk5OTk5OTk5OQ",
    ],
    ids=[
        "empty",
        "spaces",
        "symbols",
        "negative",
        "zero",
        "letters",
        "padded",
        "past-the-largest-order",
    ],
)
async def test_job_backend_list_refuses_a_cursor_it_cannot_read(
    store: _Store, after: str
) -> None:
    """A cursor no list returned raises `ValueError`."""
    # Act & Assert
    with pytest.raises(ValueError, match="cursor"):
        await _list(store, after=after)


def _expected_pages(
    matches: list[bool], ids: list[str], limit: int
) -> list[list[str]]:
    """Return the pages a list must return, scanning at most its bound.

    `matches` and `ids` run newest first, one entry per job in the index.
    """
    bound = scan_limit(limit)
    pages: list[list[str]] = []
    position = 0
    while True:
        found: list[int] = []
        scanned = 0
        while position < len(ids) and scanned < bound and len(found) <= limit:
            if matches[position]:
                found.append(position)
            scanned += 1
            position += 1
        if len(found) > limit:
            pages.append([ids[index] for index in found[:limit]])
            position = found[limit - 1] + 1
        elif scanned == bound:
            pages.append([ids[index] for index in found])
        else:
            pages.append([ids[index] for index in found])
            return pages


async def test_job_backend_list_with_a_sparse_filter_pages_in_bounded_steps(
    store: _Store,
) -> None:
    """A sparse `status` gives short pages, and their cursors reach every job.

    No page reads more than the store's bound of index entries, so a page can
    hold fewer than `limit` jobs while more follow.
    """
    # Arrange
    count, every, limit = 250, 60, 3
    ids: list[str] = []
    for index in range(count):
        record = await store.submit()
        if index % every == 0:
            await store.backend.cancel(name=NAME, job_id=record.id, owner=OWNER)
        ids.append(record.id)
    newest_first = list(reversed(ids))
    matches = [(count - 1 - index) % every == 0 for index in range(count)]
    expected = _expected_pages(matches, newest_first, limit)

    # Act
    pages: list[list[str]] = []
    after: str | None = None
    while True:
        listed = await _list(
            store, status=JobStatus.CANCELLED, limit=limit, after=after
        )
        pages.append(_ids(listed))
        if listed.next is None:
            break
        after = listed.next

    # Assert
    assert pages == expected
    assert [job_id for page in pages for job_id in page] == [
        job_id
        for job_id, matched in zip(newest_first, matches, strict=True)
        if matched
    ]
    assert any(len(page) < limit for page in pages[:-1])


async def test_job_backend_list_never_reuses_an_order_after_a_purge(
    store: _Store,
) -> None:
    """A job submitted after a page never shows on the next page.

    That holds even once the newest jobs before it are forgotten.
    """
    # Arrange
    jobs = [await store.submit(keep=timedelta(0)) for _ in range(4)]
    first = await _list(store, limit=2)
    assert _ids(first) == [jobs[3].id, jobs[2].id]
    for job in jobs[1:]:
        await store.backend.cancel(name=NAME, job_id=job.id, owner=OWNER)
    await store.backend.purge(limit=100)

    # Act
    newer = await store.submit()
    second = await _list(store, limit=2, after=first.next)

    # Assert
    assert _ids(second) == [jobs[0].id]
    assert newer.id not in _ids(second)


async def _ended(
    store: _Store,
    *,
    status: EndStatus = JobStatus.SUCCEEDED,
    **overrides: Any,  # noqa: ANN401
) -> str:
    """Store a job, end it in `status`, and return its id.

    A `cancelled` job ends by a cancel while it is queued. Any other ends by
    its runner's settle.
    """
    if status is JobStatus.CANCELLED:
        queued = await store.submit(**overrides)
        await store.backend.cancel(name=NAME, job_id=queued.id, owner=OWNER)
        return queued.id
    running = await store.submit(start_as="r", **overrides)
    error = None if status is JobStatus.SUCCEEDED else "boom"
    await _settle(store, running.id, status=status, error=error)
    return running.id


async def _kept(store: _Store, job_ids: list[str]) -> list[bool]:
    """Return whether each job still reads back."""
    return [
        await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)
        is not None
        for job_id in job_ids
    ]


async def test_job_backend_max_kept_succeeded_forgets_the_oldest(
    store: _Store,
) -> None:
    """Past `max_kept_succeeded`, the run that succeeded first is forgotten."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 2}
    first = await _ended(store, **caps)
    second = await _ended(store, **caps)

    # Act
    third = await _ended(store, **caps)

    # Assert
    assert await _kept(store, [first, second, third]) == [False, True, True]


async def test_job_backend_max_kept_failed_counts_failed_and_cancelled(
    store: _Store,
) -> None:
    """`max_kept_failed` counts every end state but a success, as one."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_failed": 2}
    failed = await _ended(store, status=JobStatus.FAILED, **caps)
    running = await store.submit(start_as="r", **caps)
    await _settle(store, running.id, status=JobStatus.CANCELLED)
    cancelled_by_runner = running.id

    # Act
    cancelled = await _ended(store, status=JobStatus.CANCELLED, **caps)

    # Assert
    assert await _kept(store, [failed, cancelled_by_runner, cancelled]) == [
        False,
        True,
        True,
    ]


async def _lists(store: _Store, _job_id: str) -> object:
    """List the owner's jobs."""
    return await _list(store)


async def _reaps(store: _Store, _job_id: str) -> object:
    """Reap overdue jobs of any name."""
    return await store.backend.reap(limit=10)


async def _claims(store: _Store, _job_id: str) -> object:
    """Claim queued jobs of `NAME`."""
    return await _claim(store)


async def _gets(store: _Store, job_id: str) -> object:
    """Read the job."""
    return await store.backend.get(name=NAME, job_id=job_id, owner=OWNER)


async def _beats(store: _Store, job_id: str) -> object:
    """Beat the job as a runner that does not hold it."""
    return await _beat(store, job_id, runner="r2")


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(_gets, id="get"),
        pytest.param(_lists, id="list"),
        pytest.param(_reaps, id="reap"),
        pytest.param(_claims, id="claim"),
        pytest.param(_beats, id="beat"),
    ],
)
async def test_job_backend_max_kept_applies_when_the_store_ends_a_job(
    store: _Store, move: Callable[[_Store, str], Awaitable[object]]
) -> None:
    """A job the store ends itself, past `max_wait`, applies its cap too."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_failed": 1}
    older = await _ended(store, status=JobStatus.FAILED, **caps)
    late = await store.submit(max_wait=store.ticks(10), **caps)
    await store.elapse(10 + MARGIN)

    # Act
    await move(store, late.id)

    # Assert
    assert await _kept(store, [older, late.id]) == [False, True]
    assert (await _read(store, late.id)).error == JOB_NOT_STARTED


@pytest.mark.parametrize("read", ["get", "cancel"])
async def test_job_backend_max_kept_zero_read_that_ends_a_job_finds_nothing(
    store: _Store, read: str
) -> None:
    """A read that ends a job whose kind keeps nothing reads it as gone."""
    # Arrange
    late = await store.submit(max_wait=store.ticks(10), max_kept_failed=0)
    await store.elapse(10 + MARGIN)

    # Act
    found = await getattr(store.backend, read)(
        name=NAME, job_id=late.id, owner=OWNER
    )

    # Assert
    assert found is None


async def test_job_backend_max_kept_caps_are_independent(store: _Store) -> None:
    """Each cap counts its own kind of end only."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 1, "max_kept_failed": 1}
    succeeded = await _ended(store, **caps)
    failed = await _ended(store, status=JobStatus.FAILED, **caps)

    # Act
    newer = await _ended(store, **caps)

    # Assert
    assert await _kept(store, [succeeded, failed, newer]) == [
        False,
        True,
        True,
    ]


@pytest.mark.parametrize(
    ("caps", "status"),
    [
        pytest.param(
            {"max_kept_succeeded": 0}, JobStatus.SUCCEEDED, id="succeeded"
        ),
        pytest.param({"max_kept_failed": 0}, JobStatus.FAILED, id="failed"),
        pytest.param(
            {"max_kept_failed": 0}, JobStatus.CANCELLED, id="cancelled"
        ),
    ],
)
async def test_job_backend_max_kept_zero_keeps_nothing_of_its_kind(
    store: _Store, caps: dict[str, int], status: EndStatus
) -> None:
    """A cap of zero forgets a run of its kind as it ends, not the others."""
    # Arrange
    other = (
        JobStatus.FAILED
        if status is JobStatus.SUCCEEDED
        else JobStatus.SUCCEEDED
    )

    # Act
    gone = await _ended(store, status=status, **caps)
    kept = await _ended(store, status=other, **caps)

    # Assert
    assert await _kept(store, [gone, kept]) == [False, True]
    assert gone not in _ids(await _list(store))


async def test_job_backend_max_kept_none_caps_nothing(store: _Store) -> None:
    """With no cap, every ended run stays while it is kept."""
    # Arrange
    ended = [await _ended(store) for _ in range(3)]
    ended += [await _ended(store, status=JobStatus.FAILED) for _ in range(3)]

    # Act
    kept = await _kept(store, ended)

    # Assert
    assert kept == [True] * 6


async def test_job_backend_max_kept_and_keep_both_apply(store: _Store) -> None:
    """A run goes at whichever limit it reaches first: its cap or `keep`."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 2, "keep": store.ticks(60)}
    ended = [await _ended(store, **caps) for _ in range(3)]

    # Act
    capped = await _kept(store, ended)
    await store.elapse(60 + MARGIN)
    expired = await _kept(store, ended)

    # Assert
    assert capped == [False, True, True]
    assert expired == [False, False, False]


async def test_job_backend_max_kept_never_hides_a_run_behind_a_shorter_keep(
    store: _Store,
) -> None:
    """An older run counts toward the cap until its own `keep` passes.

    A newer run with a shorter `keep`, as after a deploy that shortened it,
    still forgets the older run past the cap.
    """
    # Arrange
    older = await _ended(store, max_kept_succeeded=1, keep=None)
    await store.elapse(20)

    # Act
    newer = await _ended(store, max_kept_succeeded=1, keep=store.ticks(10))

    # Assert
    assert await _kept(store, [older, newer]) == [False, True]


async def test_job_backend_max_kept_skips_a_run_past_its_own_keep(
    store: _Store,
) -> None:
    """A run gone by its own `keep` no longer counts, even before a purge.

    An older run with a longer `keep` stays.
    """
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 2}
    long_kept = await _ended(store, keep=timedelta(days=1), **caps)
    short_kept = await _ended(store, keep=store.ticks(20), **caps)
    await store.elapse(20 + 2 * MARGIN)

    # Act
    newer = await _ended(store, keep=timedelta(days=1), **caps)

    # Assert
    assert await _kept(store, [long_kept, short_kept, newer]) == [
        True,
        False,
        True,
    ]


async def test_job_backend_max_kept_still_applies_once_every_run_expired(
    store: _Store,
) -> None:
    """The cap counts again from the runs that end after the last expired."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 1}
    await _ended(store, keep=store.ticks(10), **caps)
    await store.elapse(10 + 2 * MARGIN)
    older = await _ended(store, **caps)

    # Act
    newer = await _ended(store, **caps)

    # Assert
    assert await _kept(store, [older, newer]) == [False, True]


async def test_job_backend_max_kept_applies_after_runs_it_forgot_expire(
    store: _Store,
) -> None:
    """A run the cap forgot, then past its `keep`, changes nothing later."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 1, "keep": store.ticks(10)}
    first = await _ended(store, **caps)
    second = await _ended(store, **caps)
    await store.elapse(10 + 2 * MARGIN)

    # Act
    newer = await _ended(store, **caps)

    # Assert
    assert await _kept(store, [first, second, newer]) == [False, False, True]


async def test_job_backend_max_kept_never_counts_a_run_without_a_cap(
    store: _Store,
) -> None:
    """A cap counts only the runs submitted with a cap of that kind.

    A run that ended with no cap stays, as before a deploy that adds one.
    """
    # Arrange
    uncapped = await _ended(store)
    older = await _ended(store, max_kept_succeeded=1)

    # Act
    newer = await _ended(store, max_kept_succeeded=1)

    # Assert
    assert await _kept(store, [uncapped, older, newer]) == [True, False, True]


async def test_job_backend_max_kept_zero_forgets_the_runs_kept_before(
    store: _Store,
) -> None:
    """An end with a cap of zero forgets the capped runs of its kind too."""
    # Arrange
    older = await _ended(store, max_kept_succeeded=5)

    # Act
    newer = await _ended(store, max_kept_succeeded=0)

    # Assert
    assert await _kept(store, [older, newer]) == [False, False]


async def test_job_backend_max_kept_takes_the_largest_cap(
    store: _Store,
) -> None:
    """A cap of `2**63 - 1` is stored and applied like any other."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 2**63 - 1}

    # Act
    ended = [await _ended(store, **caps) for _ in range(2)]

    # Assert
    assert await _kept(store, ended) == [True, True]


async def test_job_backend_max_kept_forgotten_run_is_not_listed(
    store: _Store,
) -> None:
    """A run the cap forgot leaves the list, and a cursor past it still pages."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_failed": 1}
    queued = await store.submit(**caps)
    forgotten = await _ended(store, status=JobStatus.CANCELLED, **caps)
    first = await _list(store, limit=1)
    assert _ids(first) == [forgotten]

    # Act
    newer = await _ended(store, status=JobStatus.CANCELLED, **caps)
    second = await _list(store, limit=1, after=first.next)
    listed = await _list(store)

    # Assert
    assert _ids(second) == [queued.id]
    assert _ids(listed) == [newer, queued.id]


async def test_job_backend_max_kept_holds_under_concurrent_ends(
    store: _Store,
) -> None:
    """Ends on two replicas at once never leave more runs than the cap."""
    # Arrange
    caps: dict[str, Any] = {"max_kept_succeeded": 3}
    one, other = store.replicas
    running = [
        await store.submit(start_as=f"r{index % 2}", **caps)
        for index in range(10)
    ]

    # Act
    await asyncio.gather(
        *(
            (one if index % 2 == 0 else other).settle(
                name=NAME,
                job_id=record.id,
                runner=f"r{index % 2}",
                status=JobStatus.SUCCEEDED,
                error=None,
                result=None,
                progress=None,
            )
            for index, record in enumerate(running)
        )
    )

    # Assert
    kept = await _kept(store, [record.id for record in running])
    assert sum(kept) == 3  # noqa: PLR2004
    assert len((await _list(store)).records) == 3  # noqa: PLR2004
