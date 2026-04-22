"""Test Lock."""

import subprocess
import sys
import time
import warnings
from collections.abc import AsyncGenerator

import pytest
from anyio import WouldBlock, sleep, to_thread
from pytest_mock import MockerFixture

import grelmicro.sync as sync_mod
import grelmicro.sync.abc as abc_mod
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.sync.lock import Lock
from grelmicro.sync.memory import MemorySyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

WORKER_1 = 0
WORKER_2 = 1
WORKER_COUNT = 2

LOCK_NAME = "test_leased_lock"
BACKEND_LOCK_NAME = f"lock:{LOCK_NAME}"


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


@pytest.fixture
def locks(backend: SyncBackend) -> list[Lock]:
    """Locks of multiple workers.

    ``lease_duration`` is set to 500 ms so that reentrant / context-manager
    tests never race with lease expiry (a 10 ms lease is shorter than Python
    3.12 thread-scheduling jitter under CI load, which caused
    ``test_lock_reentrant_from_thread`` to flake). Expiry tests read
    ``config.lease_duration`` and sleep through it, so they stay correct.
    """
    return [
        Lock(
            backend=backend,
            name=LOCK_NAME,
            worker=f"worker_{i}",
            lease_duration=0.5,
            retry_interval=0.001,
        )
        for i in range(WORKER_COUNT)
    ]


@pytest.fixture
def lock(locks: list[Lock]) -> Lock:
    """Lock."""
    return locks[WORKER_1]


async def test_lock_key_prefix(backend: SyncBackend, lock: Lock) -> None:
    """Test Lock uses prefixed key on the backend."""
    # Act
    await lock.acquire()

    # Assert - backend key should be prefixed
    assert await backend.locked(name=BACKEND_LOCK_NAME) is True
    # Raw name should NOT be locked
    assert await backend.locked(name=LOCK_NAME) is False


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
    with pytest.raises(LockOwnedCheckError):
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
    with pytest.raises(LockLockedCheckError):
        await lock.locked()


# --- Non-reentrant (nested usage rejected) tests ---


async def test_lock_reentrant_context_manager(lock: Lock) -> None:
    """Test Lock nested context manager raises LockReentrantError."""
    async with lock:
        with pytest.raises(LockReentrantError):
            async with lock:
                pass


async def test_lock_reentrant_acquire(lock: Lock) -> None:
    """Test Lock nested acquire raises LockReentrantError."""
    await lock.acquire()
    with pytest.raises(LockReentrantError):
        await lock.acquire()


async def test_lock_reentrant_acquire_nowait(lock: Lock) -> None:
    """Test Lock nested acquire_nowait raises LockReentrantError."""
    await lock.acquire_nowait()
    with pytest.raises(LockReentrantError):
        await lock.acquire_nowait()


async def test_lock_reentrant_acquire_then_acquire_nowait(lock: Lock) -> None:
    """Test Lock acquire then acquire_nowait raises LockReentrantError."""
    await lock.acquire()
    with pytest.raises(LockReentrantError):
        await lock.acquire_nowait()


async def test_lock_reentrant_acquire_nowait_then_acquire(lock: Lock) -> None:
    """Test Lock acquire_nowait then acquire raises LockReentrantError."""
    await lock.acquire_nowait()
    with pytest.raises(LockReentrantError):
        await lock.acquire()


async def test_lock_reacquire_after_release(lock: Lock) -> None:
    """Test Lock can be acquired again after release."""
    await lock.acquire()
    await lock.release()
    await lock.acquire()
    assert await lock.locked() is True


async def test_lock_reacquire_after_context_manager(lock: Lock) -> None:
    """Test Lock can be acquired again after context manager exit."""
    async with lock:
        pass
    async with lock:
        assert await lock.locked() is True


async def test_lock_reentrant_from_thread(lock: Lock) -> None:
    """Test Lock nested from_thread raises LockReentrantError."""

    def sync() -> None:
        with (
            lock.from_thread,
            pytest.raises(LockReentrantError),
            lock.from_thread,
        ):
            pass

    await to_thread.run_sync(sync)


async def test_lock_reentrant_from_thread_acquire(lock: Lock) -> None:
    """Test Lock nested from_thread acquire raises LockReentrantError."""

    def sync() -> None:
        lock.from_thread.acquire()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire()

    await to_thread.run_sync(sync)


async def test_lock_reentrant_from_thread_acquire_nowait(lock: Lock) -> None:
    """Test Lock nested from_thread acquire_nowait raises LockReentrantError."""

    def sync() -> None:
        lock.from_thread.acquire_nowait()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire_nowait()

    await to_thread.run_sync(sync)


async def test_lock_reentrant_from_thread_acquire_then_acquire_nowait(
    lock: Lock,
) -> None:
    """Test Lock from_thread acquire then acquire_nowait raises LockReentrantError."""

    def sync() -> None:
        lock.from_thread.acquire()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire_nowait()

    await to_thread.run_sync(sync)


async def test_lock_reentrant_from_thread_acquire_nowait_then_acquire(
    lock: Lock,
) -> None:
    """Test Lock from_thread acquire_nowait then acquire raises LockReentrantError."""

    def sync() -> None:
        lock.from_thread.acquire_nowait()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire()

    await to_thread.run_sync(sync)


async def test_lock_from_thread_reacquire_after_release(lock: Lock) -> None:
    """Test Lock from_thread can be acquired again after release."""

    def sync() -> None:
        lock.from_thread.acquire()
        lock.from_thread.release()
        lock.from_thread.acquire()
        assert lock.from_thread.locked() is True

    await to_thread.run_sync(sync)


async def test_lock_from_thread_reacquire_after_context_manager(
    lock: Lock,
) -> None:
    """Test Lock from_thread can be acquired again after context manager exit."""

    def sync() -> None:
        with lock.from_thread:
            pass
        with lock.from_thread:
            assert lock.from_thread.locked() is True

    await to_thread.run_sync(sync)


async def test_lock_retry_interval_too_small(backend: SyncBackend) -> None:
    """Test Lock rejects retry_interval below minimum."""
    with pytest.raises(ValueError, match="retry_interval must be"):
        Lock(name="test", backend=backend, retry_interval=0.0001)


# --- Deprecated token parameter tests ---


def test_lock_acquire_error_token_deprecated() -> None:
    """Test LockAcquireError token parameter emits DeprecationWarning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        error = LockAcquireError(name="test", token="old-token")  # noqa: S106
        assert "test" in str(error)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "Remove it" in str(w[0].message)


def test_lock_acquire_error_no_token_no_warning() -> None:
    """Test LockAcquireError without token emits no warning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LockAcquireError(name="test")
        assert len(w) == 0


def test_lock_release_error_token_deprecated() -> None:
    """Test LockReleaseError token parameter emits DeprecationWarning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        error = LockReleaseError(name="test", token="old-token")  # noqa: S106
        assert "test" in str(error)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)


def test_lock_release_error_no_token_no_warning() -> None:
    """Test LockReleaseError without token emits no warning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LockReleaseError(name="test")
        assert len(w) == 0


def test_lock_not_owned_error_token_deprecated() -> None:
    """Test LockNotOwnedError token parameter emits DeprecationWarning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        error = LockNotOwnedError(name="test", token="old-token")  # noqa: S106
        assert "lock not owned" in str(error)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)


def test_lock_not_owned_error_no_token_no_warning() -> None:
    """Test LockNotOwnedError without token emits no warning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LockNotOwnedError(name="test")
        assert len(w) == 0


# --- Deprecated Synchronization alias tests ---


def test_synchronization_deprecated_alias_from_abc() -> None:
    """Test Synchronization alias emits DeprecationWarning from abc module."""
    abc_mod.__dict__.pop("Synchronization", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cls = abc_mod.Synchronization
        assert cls is abc_mod.SyncPrimitive
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "Synchronization" in str(w[0].message)


def test_synchronization_deprecated_alias_from_sync() -> None:
    """Test Synchronization alias emits DeprecationWarning from sync module."""
    sync_mod.__dict__.pop("Synchronization", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cls = sync_mod.Synchronization
        assert cls is sync_mod.SyncPrimitive
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)


def test_sync_abc_getattr_unknown() -> None:
    """Test abc __getattr__ raises AttributeError for unknown names."""
    with pytest.raises(AttributeError, match="NoSuchThing"):
        abc_mod.NoSuchThing  # noqa: B018


def test_sync_module_getattr_unknown() -> None:
    """Test sync __getattr__ raises AttributeError for unknown names."""
    with pytest.raises(AttributeError, match="NoSuchThing"):
        sync_mod.NoSuchThing  # noqa: B018


def test_synchronization_from_import_single_warning() -> None:
    """Test 'from grelmicro.sync import Synchronization' emits exactly one warning.

    Regression test: CPython's importlib._handle_fromlist calls __getattr__
    twice internally. The globals() caching prevents duplicate warnings.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "always",
            "-c",
            "from grelmicro.sync import Synchronization",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    warning_lines = [
        line
        for line in result.stderr.splitlines()
        if "DeprecationWarning" in line
    ]
    assert len(warning_lines) == 1, (
        f"Expected 1 warning, got {len(warning_lines)}: {result.stderr}"
    )
