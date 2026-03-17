"""Test Task Lock."""

import time
from collections.abc import AsyncGenerator

import pytest
from anyio import WouldBlock, sleep, to_thread
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockReleaseError,
)
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock, TaskLockConfig

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

LOCK_NAME = "test_task_lock"
LOCK_AT_LEAST_FOR = 5
LOCK_AT_MOST_FOR = 10
WORKER_1 = "worker_1"
WORKER_2 = "worker_2"


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


# --- Config Validation ---


def test_tasklock_config_valid() -> None:
    """Test TaskLockConfig with valid values."""
    config = TaskLockConfig(
        name="test",
        worker="worker",
        min_lock_seconds=LOCK_AT_LEAST_FOR,
        max_lock_seconds=LOCK_AT_MOST_FOR,
    )
    assert config.min_lock_seconds == LOCK_AT_LEAST_FOR
    assert config.max_lock_seconds == LOCK_AT_MOST_FOR


def test_tasklock_config_at_least_greater_than_at_most() -> None:
    """Test TaskLockConfig raises when min_lock_seconds > max_lock_seconds."""
    with pytest.raises(
        ValidationError, match="min_lock_seconds must be less than or equal to"
    ):
        TaskLockConfig(
            name="test",
            worker="worker",
            min_lock_seconds=10,
            max_lock_seconds=5,
        )


def test_tasklock_config_at_least_not_positive() -> None:
    """Test TaskLockConfig raises when min_lock_seconds is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            name="test",
            worker="worker",
            min_lock_seconds=0,
            max_lock_seconds=10,
        )


def test_tasklock_config_at_most_not_positive() -> None:
    """Test TaskLockConfig raises when max_lock_seconds is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            name="test",
            worker="worker",
            min_lock_seconds=1,
            max_lock_seconds=0,
        )


def test_tasklock_config_equal_values() -> None:
    """Test TaskLockConfig with min_lock_seconds == max_lock_seconds."""
    config = TaskLockConfig(
        name="test",
        worker="worker",
        min_lock_seconds=10,
        max_lock_seconds=10,
    )
    assert config.min_lock_seconds == config.max_lock_seconds


# --- Acquire + Release (elapsed >= min_lock_seconds) ---


async def test_tasklock_acquire_release(backend: SyncBackend) -> None:
    """Test TaskLock acquires and releases when elapsed >= min_lock_seconds."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )

    locked_before = await backend.locked(name=LOCK_NAME)
    async with task_lock:
        locked_inside = await backend.locked(name=LOCK_NAME)
        await sleep(0.01)  # Ensure elapsed > min_lock_seconds
    locked_after = await backend.locked(name=LOCK_NAME)

    assert locked_before is False
    assert locked_inside is True
    assert locked_after is False


# --- WouldBlock when already locked ---


async def test_tasklock_would_block(backend: SyncBackend) -> None:
    """Test TaskLock raises WouldBlock when already locked by another worker."""
    task_lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    task_lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_2,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    async with task_lock_1:
        with pytest.raises(WouldBlock):
            async with task_lock_2:
                pass  # Should not reach here


# --- Lock stays held after exit when elapsed < min_lock_seconds ---


async def test_tasklock_stays_locked_when_elapsed_less_than_at_least(
    backend: SyncBackend,
) -> None:
    """Test lock stays held after exit when elapsed < min_lock_seconds."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.5,
        max_lock_seconds=10,
    )

    async with task_lock:
        pass  # Completes almost instantly (elapsed << 0.5)

    # Lock should still be held
    locked_after = await backend.locked(name=LOCK_NAME)
    assert locked_after is True

    # Wait for min_lock_seconds to expire
    await sleep(0.6)
    locked_expired = await backend.locked(name=LOCK_NAME)
    assert locked_expired is False


# --- Lock auto-expires after max_lock_seconds ---


async def test_tasklock_auto_expires(backend: SyncBackend) -> None:
    """Test lock auto-expires after max_lock_seconds."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    async with task_lock:
        locked_inside = await backend.locked(name=LOCK_NAME)
        assert locked_inside is True
        await sleep(0.1)

    # Lock should have expired by now
    locked_after = await backend.locked(name=LOCK_NAME)
    assert locked_after is False


# --- Same worker can re-enter (re-acquire updates TTL) ---


async def test_tasklock_same_worker_reacquire(backend: SyncBackend) -> None:
    """Test same worker can re-acquire (token-based re-entrancy)."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )

    async with task_lock:
        locked = await backend.locked(name=LOCK_NAME)
        assert locked is True
        await sleep(0.01)

    # After release, acquire again
    async with task_lock:
        locked = await backend.locked(name=LOCK_NAME)
        assert locked is True


# --- Warning on expired lock ---


async def test_tasklock_release_expired_warning(
    backend: SyncBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """Test TaskLock logs warning when lock expired before release."""
    # Arrange
    caplog.set_level("WARNING")
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    # Act
    async with task_lock:
        await sleep(0.1)  # Wait for lock to expire

    # Assert
    assert any(
        "Task lock expired before release" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


# --- Backend errors ---


async def test_tasklock_acquire_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockAcquireError on backend error during acquire."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
    )
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Error")
    )

    with pytest.raises(LockAcquireError):
        async with task_lock:
            pass


async def test_tasklock_locked_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockLockedCheckError on backend error during locked check."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
    )
    mocker.patch.object(
        backend, "locked", side_effect=Exception("Backend Error")
    )

    with pytest.raises(LockLockedCheckError):
        await task_lock.locked()


async def test_tasklock_release_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockReleaseError on backend error during release."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )

    # Acquire successfully, then fail on release
    with pytest.raises(LockReleaseError):  # noqa: PT012
        async with task_lock:
            await sleep(0.01)  # Ensure elapsed > min_lock_seconds
            # Patch release after successful acquire
            mocker.patch.object(
                backend, "release", side_effect=Exception("Backend Error")
            )


async def test_tasklock_reacquire_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockReleaseError on backend error during re-acquire in exit."""
    min_lock_seconds = 10
    max_lock_seconds = 60
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=min_lock_seconds,
        max_lock_seconds=max_lock_seconds,
    )

    original_acquire = backend.acquire

    async def fail_on_reacquire(
        *, name: str, token: str, duration: float
    ) -> bool:
        # Let initial acquire succeed, fail on re-acquire (shorter duration)
        if duration < max_lock_seconds:
            msg = "Backend Error"
            raise Exception(msg)  # noqa: TRY002
        return await original_acquire(name=name, token=token, duration=duration)

    mocker.patch.object(backend, "acquire", side_effect=fail_on_reacquire)

    with pytest.raises(LockReleaseError):
        async with task_lock:
            pass


async def test_tasklock_reacquire_lost_warning(
    backend: SyncBackend,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test TaskLock logs warning when re-acquire returns False (lock lost)."""
    # Arrange
    caplog.set_level("WARNING")
    min_lock_seconds = 10
    max_lock_seconds = 60
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=min_lock_seconds,
        max_lock_seconds=max_lock_seconds,
    )

    original_acquire = backend.acquire

    async def reject_reacquire(
        *, name: str, token: str, duration: float
    ) -> bool:
        # Let initial acquire succeed, return False on re-acquire
        if duration < max_lock_seconds:
            return False
        return await original_acquire(name=name, token=token, duration=duration)

    mocker.patch.object(backend, "acquire", side_effect=reject_reacquire)

    # Act
    async with task_lock:
        pass

    # Assert
    assert any(
        "Task lock lost before re-acquire" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


# --- from_thread ---


async def test_tasklock_from_thread_acquire_release(
    backend: SyncBackend,
) -> None:
    """Test TaskLock from thread acquires and releases when elapsed >= min_lock_seconds."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )
    locked_before = False
    locked_inside = False
    locked_after = False

    def sync() -> None:
        nonlocal locked_before, locked_inside, locked_after

        locked_before = task_lock.from_thread.locked()
        with task_lock.from_thread:
            locked_inside = task_lock.from_thread.locked()
            time.sleep(0.01)  # Ensure elapsed > min_lock_seconds
        locked_after = task_lock.from_thread.locked()

    await to_thread.run_sync(sync)

    assert locked_before is False
    assert locked_inside is True
    assert locked_after is False


async def test_tasklock_from_thread_would_block(backend: SyncBackend) -> None:
    """Test TaskLock from thread raises WouldBlock when already locked."""
    task_lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    task_lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_2,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    def sync() -> None:
        with (
            task_lock_1.from_thread,
            pytest.raises(WouldBlock),
            task_lock_2.from_thread,
        ):
            pass

    await to_thread.run_sync(sync)


async def test_tasklock_from_thread_stays_locked(backend: SyncBackend) -> None:
    """Test TaskLock from thread stays locked when elapsed < min_lock_seconds."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.5,
        max_lock_seconds=10,
    )
    locked_after = False

    def sync() -> None:
        nonlocal locked_after

        with task_lock.from_thread:
            pass  # Completes almost instantly (elapsed << 0.5)
        locked_after = task_lock.from_thread.locked()

    await to_thread.run_sync(sync)

    assert locked_after is True


async def test_tasklock_from_thread_acquire_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock from thread raises LockAcquireError on backend error."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
    )
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Error")
    )

    def sync() -> None:
        with pytest.raises(LockAcquireError), task_lock.from_thread:
            pass

    await to_thread.run_sync(sync)


async def test_tasklock_from_thread_release_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock from thread raises LockReleaseError on backend error during release."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )

    def sync() -> None:
        with pytest.raises(LockReleaseError):  # noqa: PT012, SIM117
            with task_lock.from_thread:
                time.sleep(0.01)  # Ensure elapsed > min_lock_seconds
                mocker.patch.object(
                    backend, "release", side_effect=Exception("Backend Error")
                )

    await to_thread.run_sync(sync)


async def test_tasklock_from_thread_reacquire_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock from thread raises LockReleaseError on backend error during re-acquire."""
    min_lock_seconds = 10
    max_lock_seconds = 60
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=min_lock_seconds,
        max_lock_seconds=max_lock_seconds,
    )

    original_acquire = backend.acquire

    async def fail_on_reacquire(
        *, name: str, token: str, duration: float
    ) -> bool:
        if duration < max_lock_seconds:
            msg = "Backend Error"
            raise Exception(msg)  # noqa: TRY002
        return await original_acquire(name=name, token=token, duration=duration)

    mocker.patch.object(backend, "acquire", side_effect=fail_on_reacquire)

    def sync() -> None:
        with pytest.raises(LockReleaseError), task_lock.from_thread:
            pass

    await to_thread.run_sync(sync)


async def test_tasklock_from_thread_release_expired_warning(
    backend: SyncBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """Test TaskLock from thread logs warning when lock expired before release."""
    caplog.set_level("WARNING")
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    def sync() -> None:
        with task_lock.from_thread:
            time.sleep(0.1)  # Wait for lock to expire

    await to_thread.run_sync(sync)

    assert any(
        "Task lock expired before release" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )
