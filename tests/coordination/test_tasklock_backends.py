"""Test TaskLock on real stores.

`tests/coordination/test_tasklock.py` covers the behavior of the pattern on the
memory backend. These tests run what depends on the store: how it holds a lease
past the end of the body, lets it expire, and hands it back. Each store keeps
lease times its own way, and Kubernetes keeps them in whole seconds.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import timedelta
from itertools import pairwise
from uuid import uuid4

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination._protocol import LockBackend
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.kubernetes import KubernetesLockAdapter
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import WouldBlockError
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.task._interval import IntervalTask
from tests._redis_cluster import CLUSTERS, cluster_provider
from tests.task import samples
from tests.task._helpers import cancel_group, start_task

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60, func_only=True)]
"""Sixty seconds of test body, with container startup left to its own wait."""

WORKER_1 = "worker_1"
WORKER_2 = "worker_2"

LONG = 30
"""Lease in seconds for a test that must not see it end."""

INTERVAL = 1.0
"""Seconds between task runs. Kubernetes cannot hold a shorter claim."""

MIN_CLAIMED_RUNS = 4
"""Runs a claimed task must reach in its six seconds, so a lost interval shows."""

GAP_TOLERANCE = 0.8
"""Share of the interval two runs must stay apart, for a loaded machine."""


type LockSource = RedisProvider | PostgresProvider | KubernetesLockAdapter
"""What a claimed task and a TaskLock take their lock backend from."""


@pytest.fixture(
    scope="module",
    params=[
        "redis",
        "valkey",
        *CLUSTERS,
        "postgres",
        pytest.param("kubernetes", marks=[pytest.mark.xdist_group("k3s")]),
    ],
)
def lock_source(
    request: pytest.FixtureRequest,
) -> Generator[Callable[[], LockSource]]:
    """Start one store for the module, and return how to connect to it."""
    if request.param == "redis":
        with RedisContainer() as redis:
            port = redis.get_exposed_port(6379)
            yield lambda: RedisProvider(f"redis://localhost:{port}/0")
    elif request.param == "valkey":
        with RedisContainer(image="valkey/valkey:8.1") as valkey:
            port = valkey.get_exposed_port(6379)
            yield lambda: ValkeyProvider(f"redis://localhost:{port}/0")
    elif request.param in CLUSTERS:
        yield lambda: cluster_provider(request, request.param)
    elif request.param == "postgres":
        with PostgresContainer() as postgres:
            port = postgres.get_exposed_port(5432)
            yield lambda: PostgresProvider(
                f"postgresql://test:test@localhost:{port}/test"
            )
    else:
        kubeconfig = request.getfixturevalue("k3s_kubeconfig")
        yield lambda: KubernetesLockAdapter(
            namespace="default", kubeconfig=kubeconfig
        )


@pytest.fixture
async def backend(
    lock_source: Callable[[], LockSource],
) -> AsyncGenerator[LockBackend]:
    """Open the lock backend of the store."""
    source = lock_source()
    if isinstance(source, KubernetesLockAdapter):
        async with source as adapter:
            yield adapter
        return
    async with source, source.lock_backend() as adapter:
        yield adapter


def _peer(backend: LockBackend, name: str) -> TaskLock:
    """Return the other worker, which enters only once the lock is free."""
    return TaskLock(
        name,
        backend=backend,
        worker=WORKER_2,
        min_hold_duration=timedelta(milliseconds=1),
        lease_duration=LONG,
    )


@pytest.fixture
def name() -> str:
    """Return a lock name no other test holds on the shared store."""
    return f"tasklock-{uuid4().hex}"


async def test_acquire_and_release(backend: LockBackend, name: str) -> None:
    """A TaskLock holds the store while the body runs and frees it after."""
    # Arrange
    task_lock = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=timedelta(milliseconds=1),
        lease_duration=LONG,
    )

    # Act
    async with task_lock:
        locked_inside = await task_lock.locked()
        await asyncio.sleep(0.01)
    locked_after = await task_lock.locked()

    # Assert
    assert locked_inside is True
    assert locked_after is False


async def test_a_second_worker_is_refused_while_the_first_holds(
    backend: LockBackend, name: str
) -> None:
    """Only one replica runs the task."""
    # Arrange
    first = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=timedelta(milliseconds=1),
        lease_duration=LONG,
    )
    second = TaskLock(
        name,
        backend=backend,
        worker=WORKER_2,
        min_hold_duration=timedelta(milliseconds=1),
        lease_duration=LONG,
    )

    # Act and assert
    async with first:
        with pytest.raises(WouldBlockError):
            async with second:
                pytest.fail("the second worker entered a held lock")
        await asyncio.sleep(0.01)


async def test_an_early_exit_stays_held_until_the_minimum_hold_ends(
    backend: LockBackend, name: str
) -> None:
    """The minimum hold survives the store's own lease handling."""
    # Arrange
    task_lock = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=2,
        lease_duration=LONG,
    )

    # Act
    async with task_lock:
        pass
    held_after_exit = await task_lock.locked()
    await asyncio.sleep(3.5)
    async with _peer(backend, name):
        pass

    # Assert
    assert held_after_exit is True


async def test_the_lease_expires_on_its_own(
    backend: LockBackend, name: str
) -> None:
    """A crashed holder does not block the task for good."""
    # Arrange
    task_lock = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=timedelta(milliseconds=10),
        lease_duration=1,
    )

    # Act
    with pytest.raises(LockNotOwnedError):
        async with task_lock:
            await asyncio.sleep(3)

    # Assert
    async with _peer(backend, name):
        pass


async def test_the_same_worker_takes_it_back_when_its_hold_ran_out(
    backend: LockBackend, name: str
) -> None:
    """A hold that ran out on the worker's clock lets the worker back in.

    The fraction matters. A store that keeps whole seconds holds the 1.5 second
    remainder for 2 seconds, so the lease is still live when the worker returns
    at 1.6 seconds, and the worker must not skip its run because of that.
    """
    # Arrange
    task_lock = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=timedelta(seconds=1.5),
        lease_duration=LONG,
    )
    async with task_lock:
        pass
    await asyncio.sleep(1.6)

    # Act and assert
    async with task_lock:
        await asyncio.sleep(1.6)


async def test_extend_keeps_it_held_past_the_first_lease(
    backend: LockBackend, name: str
) -> None:
    """A long body is not overtaken once it extends."""
    # Arrange
    task_lock = TaskLock(
        name,
        backend=backend,
        worker=WORKER_1,
        min_hold_duration=timedelta(milliseconds=1),
        lease_duration=5,
    )

    # Act
    async with task_lock:
        await asyncio.sleep(3)
        await task_lock.extend()
        await asyncio.sleep(3)
        held_past_first_lease = await task_lock.locked()

    # Assert
    assert held_past_first_lease is True


async def test_a_claimed_task_runs_each_interval_once_across_workers(
    lock_source: Callable[[], LockSource],
) -> None:
    """Two offset workers run every interval, and never the same one twice."""
    # Arrange
    samples.run_starts = []
    task_name = f"claimed-{uuid4().hex}"
    workers = [
        IntervalTask(
            interval=timedelta(seconds=INTERVAL),
            function=samples.record_start,
            name=task_name,
            gate="claim",
        )
        for _ in range(2)
    ]

    # Act
    async with (
        Grelmicro(uses=[Coordination(lock=lock_source())]),
        asyncio.TaskGroup() as tg,
    ):
        for worker in workers:
            await start_task(tg, worker)
            await asyncio.sleep(INTERVAL / 2)
        await asyncio.sleep(INTERVAL * 5)
        cancel_group(tg)

    # Assert
    assert len(samples.run_starts) >= MIN_CLAIMED_RUNS
    gaps = [later - earlier for earlier, later in pairwise(samples.run_starts)]
    assert min(gaps) >= INTERVAL * GAP_TOLERANCE
