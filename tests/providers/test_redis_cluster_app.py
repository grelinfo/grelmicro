"""Test an app on a bare Redis or Valkey provider with a cluster URL.

The provider takes no prefix and the app wires every store from it, so the
default hash tag of each store is what keeps its keys in one slot. Every lock,
cache key, tag and job name is unique, so tests sharing a cluster never meet.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.task import Job, JobStatus, Progress, Tasks

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30, func_only=True)]
"""Thirty seconds of test body, with cluster startup left to its own wait."""

_POLL = 0.05
"""Seconds between two polls of a running job."""

_DONE = 3
"""Items the sample job reports done."""


async def _count(progress: Progress, items: int) -> dict[str, int]:
    """Report `items` done and return the count."""
    progress.total = items
    progress.advance(items)
    return {"counted": items}


@pytest.fixture(params=["redis", "valkey"])
def provider(request: pytest.FixtureRequest) -> RedisProvider:
    """Return a bare provider on the real Redis or Valkey Cluster."""
    if request.param == "redis":
        return RedisProvider(request.getfixturevalue("redis_cluster_url"))
    return ValkeyProvider(request.getfixturevalue("valkey_cluster_url"))


@pytest.fixture
async def app(
    provider: RedisProvider,
) -> AsyncGenerator[tuple[Grelmicro, Job[...]]]:
    """Open an app on the bare provider, with one job."""
    tasks = Tasks()
    job = tasks.job(name=f"count-{uuid4().hex}")(_count)
    micro = Grelmicro(uses=[provider, tasks])
    async with micro:
        yield micro, job


async def test_lock_is_held_and_freed(app: tuple[Grelmicro, Job[...]]) -> None:
    """A lock on the bare provider holds, then frees, on the cluster."""
    # Arrange
    micro, _ = app
    lock = micro.coordination.lock(f"cart-{uuid4().hex}")

    # Act
    async with lock:
        held = await lock.locked()
    freed = not await lock.locked()

    # Assert
    assert (held, freed) == (True, True)


async def test_read_write_lock_takes_a_read(
    app: tuple[Grelmicro, Job[...]],
) -> None:
    """A read-write lock runs its multi-key scripts in one slot."""
    # Arrange
    micro, _ = app
    catalog = micro.coordination.readwritelock(f"catalog-{uuid4().hex}")

    # Act
    async with catalog.read.hold(timeout=5):
        state = await catalog.state()

    # Assert
    assert state.readers == 1


async def test_cache_entry_is_read_and_dropped_by_tag(
    app: tuple[Grelmicro, Job[...]],
) -> None:
    """A tagged cache entry is read back, then dropped with its tag."""
    # Arrange
    micro, _ = app
    cache = micro.cache.ttl(ttl=60, name=f"users-{uuid4().hex}")
    key = f"user-{uuid4().hex}"
    tag = f"tag-{uuid4().hex}"
    await cache.set(key, {"name": "Alice"}, tags=[tag])

    # Act
    found = await cache.get(key)
    await cache.delete_tags(tag)
    dropped = await cache.get(key)

    # Assert
    assert (found, dropped) == ({"name": "Alice"}, None)


async def test_job_runs_end_to_end(app: tuple[Grelmicro, Job[...]]) -> None:
    """A submitted job runs on the cluster and reports its result."""
    # Arrange
    _, job = app
    owner = job.without_owner()

    # Act
    submitted = await owner.submit(_DONE)
    info = submitted
    while info.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        await asyncio.sleep(_POLL)
        found = await owner.get(submitted.id)
        assert found is not None
        info = found

    # Assert
    assert (info.status, info.done, info.result) == (
        JobStatus.SUCCEEDED,
        _DONE,
        {"counted": _DONE},
    )
