"""Test Task Lock."""

import asyncio
import time
from asyncio import sleep
from collections.abc import AsyncGenerator

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro.errors import WouldBlockError as WouldBlock
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.sync.lock import Lock, LockConfig
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock, TaskLockConfig

pytestmark = [pytest.mark.timeout(10)]

LOCK_NAME = "test_task_lock"
BACKEND_LOCK_NAME = f"tasklock:{LOCK_NAME}"
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
            worker="worker",
            min_lock_seconds=10,
            max_lock_seconds=5,
        )


def test_tasklock_config_at_least_not_positive() -> None:
    """Test TaskLockConfig raises when min_lock_seconds is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            worker="worker",
            min_lock_seconds=0,
            max_lock_seconds=10,
        )


def test_tasklock_config_at_most_not_positive() -> None:
    """Test TaskLockConfig raises when max_lock_seconds is not positive."""
    with pytest.raises(ValidationError):
        TaskLockConfig(
            worker="worker",
            min_lock_seconds=1,
            max_lock_seconds=0,
        )


def test_tasklock_config_equal_values() -> None:
    """Test TaskLockConfig with min_lock_seconds == max_lock_seconds."""
    config = TaskLockConfig(
        worker="worker",
        min_lock_seconds=10,
        max_lock_seconds=10,
    )
    assert config.min_lock_seconds == config.max_lock_seconds


# --- Namespace isolation ---


async def test_tasklock_key_prefix(backend: SyncBackend) -> None:
    """Test TaskLock uses prefixed key on the backend."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    async with task_lock:
        # Backend key should be prefixed
        assert await backend.locked(name=BACKEND_LOCK_NAME) is True
        # Raw name should NOT be locked
        assert await backend.locked(name=LOCK_NAME) is False


async def test_tasklock_no_collision_with_lock(backend: SyncBackend) -> None:
    """Test TaskLock and Lock with same name don't collide."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    lock = Lock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        lease_duration=10,
    )

    async with task_lock:
        # Lock with same name should not be locked
        assert await lock.locked() is False


# --- Nested usage guard ---


async def test_tasklock_nested_raises(backend: SyncBackend) -> None:
    """Test TaskLock raises LockReentrantError on nested async usage."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    async with task_lock:
        with pytest.raises(LockReentrantError):
            async with task_lock:
                pass


async def test_tasklock_from_thread_nested_raises(backend: SyncBackend) -> None:
    """Test TaskLock raises LockReentrantError on nested thread usage."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    def sync() -> None:
        with task_lock.from_thread, pytest.raises(LockReentrantError):  # noqa: SIM117
            with task_lock.from_thread:
                pass

    await asyncio.to_thread(sync)


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

    locked_before = await backend.locked(name=BACKEND_LOCK_NAME)
    async with task_lock:
        locked_inside = await backend.locked(name=BACKEND_LOCK_NAME)
        await sleep(0.01)  # Ensure elapsed > min_lock_seconds
    locked_after = await backend.locked(name=BACKEND_LOCK_NAME)

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
    locked_after = await backend.locked(name=BACKEND_LOCK_NAME)
    assert locked_after is True

    # Wait for min_lock_seconds to expire
    await sleep(0.6)
    locked_expired = await backend.locked(name=BACKEND_LOCK_NAME)
    assert locked_expired is False


# --- Lock auto-expires after max_lock_seconds ---


async def test_tasklock_auto_expires(backend: SyncBackend) -> None:
    """Test lock auto-expires after max_lock_seconds and raises on release."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with task_lock:
            locked_inside = await backend.locked(name=BACKEND_LOCK_NAME)
            assert locked_inside is True
            await sleep(0.1)

    # Lock should have expired by now
    locked_after = await backend.locked(name=BACKEND_LOCK_NAME)
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
        locked = await backend.locked(name=BACKEND_LOCK_NAME)
        assert locked is True
        await sleep(0.01)

    # After release, acquire again
    async with task_lock:
        locked = await backend.locked(name=BACKEND_LOCK_NAME)
        assert locked is True


# --- Warning on expired lock ---


async def test_tasklock_release_expired_raises(
    backend: SyncBackend,
) -> None:
    """Test TaskLock raises LockNotOwnedError when lock expired before release."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    with pytest.raises(LockNotOwnedError):
        async with task_lock:
            await sleep(0.1)  # Wait for lock to expire


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


async def test_tasklock_state_cleaned_up_after_failed_reacquire(
    backend: SyncBackend, mocker: MockerFixture
) -> None:
    """Test TaskLock clears state even when re-acquire fails with an exception.

    do_exit() must clear _acquired_at and rotate the nonce before calling
    the backend. This ensures the lock instance is always left in a clean
    state, regardless of backend errors. The TTL (max_lock_seconds) acts
    as deadlock protection for the backend-side lock.
    """
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
            msg = "Transient backend error"
            raise Exception(msg)  # noqa: TRY002
        return await original_acquire(name=name, token=token, duration=duration)

    mocker.patch.object(backend, "acquire", side_effect=fail_on_reacquire)

    # First usage: acquire succeeds, re-acquire in exit fails
    with pytest.raises(LockReleaseError):
        async with task_lock:
            pass

    # State is cleaned up despite the error
    assert task_lock._acquired_at is None


async def test_tasklock_reacquire_lost_raises(
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """Test TaskLock raises LockNotOwnedError when re-acquire returns False."""
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

    with pytest.raises(LockNotOwnedError):
        async with task_lock:
            pass


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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)


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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)


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

    await asyncio.to_thread(sync)


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

    await asyncio.to_thread(sync)


async def test_tasklock_from_thread_release_expired_raises(
    backend: SyncBackend,
) -> None:
    """Test TaskLock from thread raises LockNotOwnedError when lock expired."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=0.01,
        max_lock_seconds=0.05,
    )

    def sync() -> None:
        with pytest.raises(LockNotOwnedError):  # noqa: SIM117
            with task_lock.from_thread:
                time.sleep(0.1)  # Wait for lock to expire

    await asyncio.to_thread(sync)


async def test_task_lock_config_property(backend: SyncBackend) -> None:
    """Test TaskLock config property returns the config."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    expected_min = 1
    expected_max = 10
    assert task_lock.name == LOCK_NAME
    config = task_lock.config
    assert config.min_lock_seconds == expected_min
    assert config.max_lock_seconds == expected_max


async def test_task_lock_exit_without_acquire(backend: SyncBackend) -> None:
    """Test TaskLock exit without acquire raises LockNotOwnedError."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    with pytest.raises(LockNotOwnedError):
        await task_lock.__aexit__(None, None, None)


# --- reconfigure ---


async def test_tasklock_reconfigure_swaps_config(backend: SyncBackend) -> None:
    """Reconfigure publishes the new config."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    new_config = task_lock.config.model_copy(
        update={"min_lock_seconds": 2, "max_lock_seconds": 20},
    )

    await task_lock.reconfigure(new_config)

    assert task_lock.config == new_config


async def test_tasklock_reconfigure_same_config_is_noop(
    backend: SyncBackend,
) -> None:
    """Equal configs short-circuit."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    same = task_lock.config.model_copy()

    await task_lock.reconfigure(same)

    assert task_lock.config == same


async def test_tasklock_reconfigure_rejects_worker_change(
    backend: SyncBackend,
) -> None:
    """Changing `worker` is not allowed: it is part of the live token."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    new_config = task_lock.config.model_copy(update={"worker": WORKER_2})

    with pytest.raises(ValueError, match="cannot change worker"):
        await task_lock.reconfigure(new_config)


async def test_tasklock_reconfigure_changes_max_lock_for_next_acquire(
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """Acquire after reconfigure passes the new max_lock_seconds to the backend."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    spy = mocker.spy(backend, "acquire")
    new_config = task_lock.config.model_copy(update={"max_lock_seconds": 42})

    await task_lock.reconfigure(new_config)
    async with task_lock:
        pass

    # First call is the entry acquire (uses max_lock_seconds);
    # the second is the exit re-acquire that holds the lock until
    # min_lock_seconds elapsed.
    assert spy.call_args_list[0].kwargs["duration"] == 42  # noqa: PLR2004


async def test_tasklock_reconfigure_rejects_different_config_type(
    backend: SyncBackend,
) -> None:
    """The mixin rejects config types different from the current one."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=WORKER_1,
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    with pytest.raises(TypeError, match="TaskLockConfig"):
        await task_lock.reconfigure(LockConfig(worker=WORKER_1))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
