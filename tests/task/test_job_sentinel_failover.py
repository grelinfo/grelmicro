"""Test a running job across a real Redis Sentinel failover.

Each test starts its own Sentinel set, kills the master, and waits for
Sentinel to promote the replica. Redis copies a write to its replica after it
acknowledges it, so whether a claim survives depends on whether it reached the
replica first. A lost claim leaves the job queued, so another replica can run
its body a second time, as the docs warn.
"""

from collections.abc import AsyncGenerator, Generator
from datetime import timedelta

import pytest
from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel

from grelmicro.providers.redis import RedisProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import Beat, JobBackend, JobRecord
from tests._redis_cluster import unique_prefix
from tests._redis_sentinel import SERVICE, SentinelSet, start_sentinel_set

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(120, func_only=True),
]

NAME = "app:import_rows"

LEASE = timedelta(seconds=60)
"""A lease that outlasts the failover."""


@pytest.fixture
def sentinel_set() -> Generator[SentinelSet]:
    """Start a Sentinel set this test alone fails over."""
    with start_sentinel_set() as started:
        yield started


@pytest.fixture
async def connected(
    sentinel_set: SentinelSet,
) -> AsyncGenerator[tuple[Sentinel, Redis, JobBackend]]:
    """Yield the Sentinel client, the master client and an open job store."""
    sentinel = sentinel_set.sentinel()
    client = sentinel.master_for(SERVICE)
    provider = RedisProvider.from_client(client, own=True)
    backend = provider.job_backend(prefix=unique_prefix())
    async with provider, backend:
        yield sentinel, client, backend


async def _submit(backend: JobBackend) -> JobRecord:
    """Store a queued job, and return its record."""
    submitted = await backend.submit(
        name=NAME,
        job_id="job-1",
        owner=None,
        payload="{}",
        context={},
        max_queued=None,
        max_wait=None,
        keep=timedelta(hours=1),
        start_as=None,
        lease=LEASE,
    )
    assert submitted.record is not None
    return submitted.record


async def _status(backend: JobBackend, job: JobRecord) -> JobStatus:
    """Return the status the store reads for `job`."""
    record = await backend.get(name=NAME, job_id=job.id, owner=None)
    assert record is not None
    return record.status


async def test_a_replicated_claim_survives_the_promotion(
    sentinel_set: SentinelSet,
    connected: tuple[Sentinel, Redis, JobBackend],
) -> None:
    """A claim the replica got reads `running`, and its runner keeps it."""
    # Arrange
    sentinel, client, backend = connected
    job = await _submit(backend)
    claimed = await backend.claim(slots={NAME: 1}, runner="r1", lease=LEASE)
    assert [c.record.id for c in claimed] == [job.id]
    assert await client.execute_command("WAIT", 1, 2000) == 1

    # Act
    sentinel_set.kill_master()
    await sentinel_set.wait_for_promotion(sentinel, client)

    # Assert
    assert await _status(backend, job) is JobStatus.RUNNING
    replies = await backend.beat(
        runner="r1", beats=[Beat(name=NAME, job_id=job.id)], lease=LEASE
    )
    assert [reply.held for reply in replies] == [True]


async def test_an_unreplicated_claim_is_lost_and_the_job_runs_again(
    sentinel_set: SentinelSet,
    connected: tuple[Sentinel, Redis, JobBackend],
) -> None:
    """A claim the replica never got is lost, so a second claim takes the job."""
    # Arrange
    sentinel, client, backend = connected
    job = await _submit(backend)
    assert await client.execute_command("WAIT", 1, 2000) == 1
    await sentinel_set.cut_replica(client)
    claimed = await backend.claim(slots={NAME: 1}, runner="r1", lease=LEASE)
    assert [c.record.id for c in claimed] == [job.id]
    assert await client.execute_command("WAIT", 1, 500) == 0

    # Act
    sentinel_set.kill_master()
    sentinel_set.resume_replica()
    await sentinel_set.wait_for_promotion(sentinel, client)

    # Assert
    assert await _status(backend, job) is JobStatus.QUEUED
    again = await backend.claim(slots={NAME: 1}, runner="r2", lease=LEASE)
    assert [c.record.id for c in again] == [job.id]
    replies = await backend.beat(
        runner="r1", beats=[Beat(name=NAME, job_id=job.id)], lease=LEASE
    )
    assert [reply.held for reply in replies] == [False]
