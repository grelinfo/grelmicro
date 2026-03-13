"""Test Task Lock."""

from collections.abc import AsyncGenerator

import pytest
from anyio import WouldBlock, sleep
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import LockAcquireError, LockReleaseError
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock, TaskLockConfig

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

LOCK_NAME = "test_task_lock"
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
        lock_at_least_for=5,
        lock_at_most_for=10,
    )
    assert config.lock_at_least_for == 5
    assert config.lock_at_most_for == 10


def test_tasklock_config_at_least_greater_than_at_most() -> None:
    """Test TaskLockConfig raises when lock_at_least_for > lock_at_most_for."""
    with pytest.raises(
        ValidationError, match="lock_at_least_for must be less than or equal to"
    ):
        TaskLockConfig(
            name="test",
            worker="worker",
            lock_at_least_for=10,
            lock_at_most_for=5,
        )


def test_tasklock_config_at_least_not_positive() -> None:
    """Test TaskLockConfig raises when lock_at_least_for is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            name="test",
            worker="worker",
            lock_at_least_for=0,
            lock_at_most_for=10,
        )


def test_tasklock_config_at_most_not_positive() -> None:
    """Test TaskLockConfig raises when lock_at_most_for is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            name="test",
            worker="worker",
            lock_at_least_for=1,
            lock_at_most_for=0,
        )


def test_tasklock_config_equal_values() -> None:
    """Test TaskLockConfig with lock_at_least_for == lock_at_most_for."""
    config = TaskLockConfig(
        name="test",
        worker="worker",
        lock_at_least_for=10,
        lock_at_most_for=10,
    )
    assert config.lock_at_least_for == config.lock_at_most_for


# --- Acquire + Release (elapsed >= lock_at_least_for) ---


async def test_tasklock_acquire_release(backend: SyncBackend) -> None:
    """Test TaskLock acquires and releases when elapsed >= lock_at_least_for."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=0.001,
        lock_at_most_for=10,
    )

    locked_before = await backend.locked(name=LOCK_NAME)
    async with task_lock:
        locked_inside = await backend.locked(name=LOCK_NAME)
        await sleep(0.01)  # Ensure elapsed > lock_at_least_for
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
        lock_at_least_for=1,
        lock_at_most_for=10,
    )
    task_lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_2,
        lock_at_least_for=1,
        lock_at_most_for=10,
    )

    async with task_lock_1:
        with pytest.raises(WouldBlock):
            async with task_lock_2:
                pass  # Should not reach here


# --- Lock stays held after exit when elapsed < lock_at_least_for ---


async def test_tasklock_stays_locked_when_elapsed_less_than_at_least(
    backend: SyncBackend,
) -> None:
    """Test lock stays held after exit when elapsed < lock_at_least_for."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=0.5,
        lock_at_most_for=10,
    )

    async with task_lock:
        pass  # Completes almost instantly (elapsed << 0.5)

    # Lock should still be held
    locked_after = await backend.locked(name=LOCK_NAME)
    assert locked_after is True

    # Wait for lock_at_least_for to expire
    await sleep(0.6)
    locked_expired = await backend.locked(name=LOCK_NAME)
    assert locked_expired is False


# --- Lock auto-expires after lock_at_most_for ---


async def test_tasklock_auto_expires(backend: SyncBackend) -> None:
    """Test lock auto-expires after lock_at_most_for."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=0.01,
        lock_at_most_for=0.05,
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
        lock_at_least_for=0.001,
        lock_at_most_for=10,
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
        lock_at_least_for=0.01,
        lock_at_most_for=0.05,
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


async def test_tasklock_release_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockReleaseError on backend error during release."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=0.001,
        lock_at_most_for=10,
    )

    # Acquire successfully, then fail on release
    with pytest.raises(LockReleaseError):
        async with task_lock:
            await sleep(0.01)  # Ensure elapsed > lock_at_least_for
            # Patch release after successful acquire
            mocker.patch.object(
                backend, "release", side_effect=Exception("Backend Error")
            )


async def test_tasklock_reacquire_backend_error(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock raises LockReleaseError on backend error during re-acquire in exit."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=10,
        lock_at_most_for=60,
    )

    original_acquire = backend.acquire

    async def fail_on_reacquire(
        *, name: str, token: str, duration: float
    ) -> bool:
        # Let initial acquire succeed, fail on re-acquire (shorter duration)
        if duration < 60:
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
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lock_at_least_for=10,
        lock_at_most_for=60,
    )

    original_acquire = backend.acquire

    async def reject_reacquire(
        *, name: str, token: str, duration: float
    ) -> bool:
        # Let initial acquire succeed, return False on re-acquire
        if duration < 60:
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
