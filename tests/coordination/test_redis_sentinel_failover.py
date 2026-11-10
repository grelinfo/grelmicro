"""Test a `Lock` across a real Redis Sentinel failover.

Each test starts its own Sentinel set, kills the master, and waits for
Sentinel to promote the replica. Redis copies a write to its replica after it
acknowledges it, so what the new master holds depends on whether the write
reached the replica first.
"""

from collections.abc import AsyncGenerator, Generator
from datetime import timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel

from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.errors import WouldBlockError
from grelmicro.providers.redis import RedisProvider
from tests._redis_cluster import unique_prefix
from tests._redis_sentinel import SERVICE, SentinelSet, start_sentinel_set

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(120, func_only=True),
]

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
) -> AsyncGenerator[tuple[Sentinel, Redis, RedisLockAdapter]]:
    """Yield the Sentinel client, the master client and an open lock backend."""
    sentinel = sentinel_set.sentinel()
    client = sentinel.master_for(SERVICE)
    provider = RedisProvider.from_client(client, own=True)
    async with (
        provider,
        RedisLockAdapter(provider=provider, prefix=unique_prefix()) as backend,
    ):
        yield sentinel, client, backend


def _lock(name: str, backend: RedisLockAdapter, worker: str) -> Lock:
    """Return a lock on `backend` that `worker` holds."""
    return Lock(
        name,
        backend=backend,
        worker=worker,
        lease_duration=LEASE,
        env_load=False,
    )


async def test_a_replicated_acquire_is_still_held_on_the_new_master(
    sentinel_set: SentinelSet,
    connected: tuple[Sentinel, Redis, RedisLockAdapter],
) -> None:
    """The holder keeps the lock and its fencing token after a promotion."""
    # Arrange
    sentinel, client, backend = connected
    name = f"failover-{uuid4().hex}"
    mine = _lock(name, backend, "worker_0")
    theirs = _lock(name, backend, "worker_1")
    held = await mine.acquire()
    assert await client.execute_command("WAIT", 1, 2000) == 1

    # Act
    sentinel_set.kill_master()
    await sentinel_set.wait_for_promotion(sentinel, client)

    # Assert
    assert await mine.owned() is True
    extended = await mine.extend()
    assert extended.fencing_token == held.fencing_token
    with pytest.raises(WouldBlockError):
        await theirs.acquire_nowait()
    await mine.release()
    assert await mine.locked() is False


async def test_an_unreplicated_acquire_is_lost_on_the_new_master(
    sentinel_set: SentinelSet,
    connected: tuple[Sentinel, Redis, RedisLockAdapter],
) -> None:
    """A promotion loses an acquire the replica never got, and its token.

    The lock reads free on the new master, so another holder takes it with the
    same fencing token. The first holder learns it at its next extend.
    """
    # Arrange
    sentinel, client, backend = connected
    name = f"failover-{uuid4().hex}"
    mine = _lock(name, backend, "worker_0")
    theirs = _lock(name, backend, "worker_1")
    await sentinel_set.cut_replica(client)
    held = await mine.acquire()
    assert await client.execute_command("WAIT", 1, 500) == 0

    # Act
    sentinel_set.kill_master()
    sentinel_set.resume_replica()
    await sentinel_set.wait_for_promotion(sentinel, client)

    # Assert
    assert await mine.owned() is False
    taken = await theirs.acquire_nowait()
    assert taken.fencing_token == held.fencing_token
    with pytest.raises(LockNotOwnedError):
        await mine.extend()
