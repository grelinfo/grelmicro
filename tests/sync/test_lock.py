"""Test Lock."""

import time
from collections.abc import AsyncGenerator

import pytest
from anyio import WouldBlock, sleep, to_thread
from pytest_mock import MockerFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockNotOwnedError,
    LockReleaseError,
    SyncBackendError,
)
from grelmicro.sync.lock import Lock
from grelmicro.sync.memory import MemorySyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

WORKER_1 = 0
WORKER_2 = 1
WORKER_COUNT = 2

LOCK_NAME = "test_leased_lock"


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


@pytest.fixture
def locks(backend: SyncBackend) -> list[Lock]:
    """Locks of multiple workers."""
    return [
        Lock(
            backend=backend,
            name=LOCK_NAME,
            worker=f"worker_{i}",
            lease_duration=0.01,
            retry_interval=0.001,
        )
        for i in range(WORKER_COUNT)
    ]


@pytest.fixture
def lock(locks: list[Lock]) -> Lock:
    """Lock."""
    return locks[WORKER_1]


async def test_lock_owned(locks: list[Lock]) -> None:
    """Test Lock owned."""
    # Act
    worker_1_owned_before = await locks[WORKER_1].owned()
    worker_2_owned_before = await locks[WORKER_2].owned()
    await locks[WORKER_1].acquire()
    worker_1_owned_after = await locks[WORKER_1].owned()
    worker_2_owned_after = await locks[WORKER_2].owned()

    # Assert
    assert worker_1_owned_before is False
    assert worker_2_owned_before is False
    assert worker_1_owned_after is True
    assert worker_2_owned_after is False


async def test_lock_from_thread_owned(locks: list[Lock]) -> None:
    """Test Lock from thread owned."""
    # Arrange
    worker_1_owned_before = None
    worker_2_owned_before = None
    worker_1_owned_after = None
    worker_2_owned_after = None

    # Act
    def sync() -> None:
        nonlocal worker_1_owned_before
        nonlocal worker_2_owned_before
        nonlocal worker_1_owned_after
        nonlocal worker_2_owned_after

        worker_1_owned_before = locks[WORKER_1].from_thread.owned()
        worker_2_owned_before = locks[WORKER_2].from_thread.owned()
        locks[WORKER_1].from_thread.acquire()
        worker_1_owned_after = locks[WORKER_1].from_thread.owned()
        worker_2_owned_after = locks[WORKER_2].from_thread.owned()

    await to_thread.run_sync(sync)

    # Assert
    assert worker_1_owned_before is False
    assert worker_2_owned_before is False
    assert worker_1_owned_after is True
    assert worker_2_owned_after is False


async def test_lock_context_manager(lock: Lock) -> None:
    """Test Lock context manager."""
    # Act
    locked_before = await lock.locked()
    async with lock:
        locked_inside = await lock.locked()
    locked_after = await lock.locked()

    # Assert
    assert locked_before is False
    assert locked_inside is True
    assert locked_after is False


async def test_lock_from_thread_context_manager_acquire(lock: Lock) -> None:
    """Test Lock from thread context manager."""
    # Arrange
    locked_before = None
    locked_inside = None
    locked_after = None

    # Act
    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_inside
        nonlocal locked_after

        locked_before = lock.from_thread.locked()
        with lock.from_thread:
            locked_inside = lock.from_thread.locked()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is False
    assert locked_inside is True
    assert locked_after is False


async def test_lock_context_manager_wait(lock: Lock, locks: list[Lock]) -> None:
    """Test Lock context manager wait."""
    # Arrange
    await locks[WORKER_1].acquire()

    # Act
    locked_before = await lock.locked()
    async with locks[WORKER_2]:  # Wait until lock expires
        locked_inside = await lock.locked()
    locked_after = await lock.locked()

    # Assert
    assert locked_before is True
    assert locked_inside is True
    assert locked_after is False


async def test_lock_from_thread_context_manager_wait(
    lock: Lock, locks: list[Lock]
) -> None:
    """Test Lock from thread context manager wait."""
    # Arrange
    locked_before = None
    locked_inside = None
    locked_after = None
    await locks[WORKER_1].acquire()

    # Act
    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_inside
        nonlocal locked_after

        locked_before = lock.from_thread.locked()
        with locks[WORKER_2].from_thread:
            locked_inside = lock.from_thread.locked()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is True
    assert locked_inside is True
    assert locked_after is False


async def test_lock_acquire(lock: Lock) -> None:
    """Test Lock acquire."""
    # Act
    locked_before = await lock.locked()
    await lock.acquire()
    locked_after = await lock.locked()

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_lock_from_thread_acquire(lock: Lock) -> None:
    """Test Lock from thread acquire."""
    # Arrange
    locked_before = None
    locked_after = None

    # Act
    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_after

        locked_before = lock.from_thread.locked()
        lock.from_thread.acquire()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_lock_acquire_wait(lock: Lock, locks: list[Lock]) -> None:
    """Test Lock acquire wait."""
    # Arrange
    await locks[WORKER_1].acquire()

    # Act
    locked_before = await lock.locked()
    await locks[WORKER_2].acquire()  # Wait until lock expires
    locked_after = await lock.locked()

    # Assert
    assert locked_before is True
    assert locked_after is True


async def test_lock_from_thread_acquire_wait(lock: Lock) -> None:
    """Test Lock from thread acquire wait."""
    # Arrange
    locked_before = None
    locked_after = None

    # Act
    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_after

        locked_before = lock.from_thread.locked()
        lock.from_thread.acquire()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_lock_acquire_nowait(lock: Lock) -> None:
    """Test Lock wait acquire."""
    # Act
    locked_before = await lock.locked()
    await lock.acquire_nowait()
    locked_after = await lock.locked()

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_lock_from_thread_acquire_nowait(lock: Lock) -> None:
    """Test Lock from thread wait acquire."""
    # Arrange
    locked_before = None
    locked_after = None

    # Act
    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_after

        locked_before = lock.from_thread.locked()
        lock.from_thread.acquire_nowait()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_lock_acquire_nowait_would_block(locks: list[Lock]) -> None:
    """Test Lock wait acquire would block."""
    # Arrange
    await locks[WORKER_1].acquire()

    # Act / Assert
    with pytest.raises(WouldBlock):
        await locks[WORKER_2].acquire_nowait()


async def test_lock_from_thread_acquire_nowait_would_block(
    locks: list[Lock],
) -> None:
    """Test Lock from thread wait acquire would block."""
    # Arrange
    await locks[WORKER_1].acquire()

    # Act / Assert
    def sync() -> None:
        with pytest.raises(WouldBlock):
            locks[WORKER_2].from_thread.acquire_nowait()

    await to_thread.run_sync(sync)


async def test_lock_release(lock: Lock) -> None:
    """Test Lock release."""
    # Act / Assert
    with pytest.raises(LockNotOwnedError):
        await lock.release()


async def test_lock_from_thread_release(lock: Lock) -> None:
    """Test Lock from thread release."""

    # Act / Assert
    def sync() -> None:
        with pytest.raises(LockNotOwnedError):
            lock.from_thread.release()

    await to_thread.run_sync(sync)


async def test_lock_release_acquired(lock: Lock) -> None:
    """Test Lock release acquired."""
    # Arrange
    await lock.acquire()

    # Act
    locked_before = await lock.locked()
    await lock.release()
    locked_after = await lock.locked()

    # Assert
    assert locked_before is True
    assert locked_after is False


async def test_lock_from_thread_release_acquired(lock: Lock) -> None:
    """Test Lock from thread release acquired."""
    # Arrange
    locked_before = None
    locked_after = None

    def sync() -> None:
        nonlocal locked_before
        nonlocal locked_after

        lock.from_thread.acquire()

        # Act
        locked_before = lock.from_thread.locked()
        lock.from_thread.release()
        locked_after = lock.from_thread.locked()

    await to_thread.run_sync(sync)

    # Assert
    assert locked_before is True
    assert locked_after is False


async def test_lock_release_expired(locks: list[Lock]) -> None:
    """Test Lock release expired."""
    # Arrange
    await locks[WORKER_1].acquire()
    await sleep(locks[WORKER_1].config.lease_duration)

    # Act
    worker_1_locked_before = await locks[WORKER_1].locked()
    with pytest.raises(LockNotOwnedError):
        await locks[WORKER_2].release()

    # Assert
    assert worker_1_locked_before is False


async def test_lock_from_thread_release_expired(locks: list[Lock]) -> None:
    """Test Lock from thread release expired."""
    # Arrange
    worker_1_locked_before = None

    def sync() -> None:
        nonlocal worker_1_locked_before

        locks[WORKER_1].from_thread.acquire()
        time.sleep(locks[WORKER_1].config.lease_duration)

        # Act
        worker_1_locked_before = locks[WORKER_1].from_thread.locked()
        with pytest.raises(LockNotOwnedError):
            locks[WORKER_2].from_thread.release()

    await to_thread.run_sync(sync)

    # Assert
    assert worker_1_locked_before is False


async def test_lock_acquire_backend_error(
    backend: SyncBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """Test Lock acquire backend error."""
    # Arrange
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Error")
    )

    # Act
    with pytest.raises(LockAcquireError):
        await lock.acquire()


async def test_lock_from_thread_acquire_backend_error(
    backend: SyncBackend,
    lock: Lock,
    mocker: MockerFixture,
) -> None:
    """Test Lock from thread acquire backend error."""
    # Arrange
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Error")
    )

    # Act
    def sync() -> None:
        with pytest.raises(LockAcquireError):
            lock.from_thread.acquire()

    await to_thread.run_sync(sync)


async def test_lock_release_backend_error(
    backend: SyncBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """Test Lock release backend error."""
    # Arrange
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Error")
    )

    # Act
    await lock.acquire()
    with pytest.raises(LockReleaseError):
        await lock.release()


async def test_lock_from_thread_release_backend_error(
    backend: SyncBackend,
    lock: Lock,
    mocker: MockerFixture,
) -> None:
    """Test Lock from thread release backend error."""
    # Arrange
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Error")
    )

    # Act
    def sync() -> None:
        lock.from_thread.acquire()
        with pytest.raises(LockReleaseError):
            lock.from_thread.release()

    await to_thread.run_sync(sync)


async def test_lock_owned_backend_error(
    backend: SyncBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """Test Lock owned backend error."""
    # Arrange
    mocker.patch.object(
        backend, "owned", side_effect=Exception("Backend Error")
    )

    # Act / Assert
    with pytest.raises(SyncBackendError):
        await lock.owned()


async def test_lock_locked_backend_error(
    backend: SyncBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """Test Lock locked backend error."""
    # Arrange
    mocker.patch.object(
        backend, "locked", side_effect=Exception("Backend Error")
    )

    # Act / Assert
    with pytest.raises(SyncBackendError):
        await lock.locked()
