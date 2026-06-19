"""Test Lock."""

import asyncio
import time
from asyncio import sleep
from collections.abc import AsyncGenerator
from threading import get_ident

import pytest
from pytest_mock import MockerFixture

import grelmicro.coordination._base as base_module
import grelmicro.coordination.lock as lock_module
from grelmicro.coordination._handle import LockHandle
from grelmicro.coordination.abc import LockBackend
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import OutOfContextError
from grelmicro.errors import WouldBlockError as WouldBlock
from tests._faults import cancel_midflight

pytestmark = [pytest.mark.timeout(1)]

WORKER_1 = 0
WORKER_2 = 1
WORKER_COUNT = 2

LOCK_NAME = "test_leased_lock"
BACKEND_LOCK_NAME = f"lock:{LOCK_NAME}"


@pytest.fixture
async def backend() -> AsyncGenerator[LockBackend]:
    """Return Memory Synchronization Backend."""
    async with MemoryLockAdapter() as backend:
        yield backend


@pytest.fixture
async def locks(backend: LockBackend) -> list[Lock]:
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
async def lock(locks: list[Lock]) -> Lock:
    """Lock."""
    return locks[WORKER_1]


@pytest.fixture
async def reentrant_thread_lock(backend: LockBackend) -> Lock:
    """Lock with a lease long enough to outlive thread-scheduling jitter.

    The reentrant-from-thread tests do *acquire → attempt-reacquire-raises →
    release* in a single worker thread. On Python 3.12 under CI load that
    sequence can exceed a 10 ms lease, which makes the outer release race
    with lease expiry and spuriously raise ``LockNotOwnedError``. Use this
    fixture only for tests that need the slow-path guarantee; the default
    ``lock`` fixture keeps its 10 ms lease for expiry tests and the rest.
    """
    return Lock(
        backend=backend,
        name=LOCK_NAME,
        worker=f"worker_{WORKER_1}",
        lease_duration=0.5,
        retry_interval=0.001,
    )


async def test_lock_key_prefix(backend: LockBackend, lock: Lock) -> None:
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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)


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

    await asyncio.to_thread(sync)


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

    await asyncio.to_thread(sync)

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

    await asyncio.to_thread(sync)

    # Assert
    assert worker_1_locked_before is False


async def test_lock_acquire_backend_error(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
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
    backend: LockBackend,
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

    await asyncio.to_thread(sync)


async def test_lock_release_backend_error(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
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
    backend: LockBackend,
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

    await asyncio.to_thread(sync)


async def test_lock_owned_backend_error(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
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
    backend: LockBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """Test Lock locked backend error."""
    # Arrange
    mocker.patch.object(
        backend, "locked", side_effect=Exception("Backend Error")
    )

    # Act / Assert
    with pytest.raises(LockLockedCheckError):
        await lock.locked()


# --- Fencing tokens ---


async def test_acquire_returns_handle(lock: Lock) -> None:
    """`acquire` returns a LockHandle with name, token, and fencing token."""
    handle = await lock.acquire()

    assert isinstance(handle, LockHandle)
    assert handle.name == LOCK_NAME
    assert handle.token
    assert handle.fencing_token >= 1


async def test_acquire_nowait_returns_handle(lock: Lock) -> None:
    """`acquire_nowait` returns a LockHandle."""
    handle = await lock.acquire_nowait()

    assert isinstance(handle, LockHandle)
    assert handle.fencing_token >= 1


async def test_context_manager_binds_handle(lock: Lock) -> None:
    """`async with lock as held` binds the LockHandle."""
    async with lock as held:
        assert isinstance(held, LockHandle)
        assert held.fencing_token >= 1


async def test_from_thread_acquire_returns_handle(lock: Lock) -> None:
    """`from_thread.acquire` returns a LockHandle."""
    handles: list[LockHandle] = []

    def sync() -> None:
        handles.append(lock.from_thread.acquire())

    await asyncio.to_thread(sync)

    assert isinstance(handles[0], LockHandle)
    assert handles[0].fencing_token >= 1


async def test_from_thread_acquire_nowait_returns_handle(lock: Lock) -> None:
    """`from_thread.acquire_nowait` returns a LockHandle."""
    handles: list[LockHandle] = []

    def sync() -> None:
        handles.append(lock.from_thread.acquire_nowait())

    await asyncio.to_thread(sync)

    assert isinstance(handles[0], LockHandle)


async def test_from_thread_context_manager_binds_handle(lock: Lock) -> None:
    """`with lock.from_thread as held` binds the LockHandle."""
    handles: list[LockHandle] = []

    def sync() -> None:
        with lock.from_thread as held:
            handles.append(held)

    await asyncio.to_thread(sync)

    assert isinstance(handles[0], LockHandle)


async def test_fencing_token_climbs_across_reacquire(lock: Lock) -> None:
    """Releasing and re-acquiring mints a strictly greater fencing token."""
    first = await lock.acquire()
    await lock.release()
    second = await lock.acquire()

    assert second.fencing_token > first.fencing_token


async def test_blocked_acquire_then_takeover_climbs(
    locks: list[Lock],
) -> None:
    """A waiter that takes over after expiry gets a greater fencing token."""
    first = await locks[WORKER_1].acquire()
    # worker 2 blocks until worker 1's short lease expires, then takes over.
    second = await locks[WORKER_2].acquire()

    assert second.fencing_token > first.fencing_token


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


async def test_lock_reentrant_from_thread(reentrant_thread_lock: Lock) -> None:
    """Test Lock nested from_thread raises LockReentrantError."""
    lock = reentrant_thread_lock

    def sync() -> None:
        with (
            lock.from_thread,
            pytest.raises(LockReentrantError),
            lock.from_thread,
        ):
            pass

    await asyncio.to_thread(sync)


async def test_lock_reentrant_from_thread_acquire(
    reentrant_thread_lock: Lock,
) -> None:
    """Test Lock nested from_thread acquire raises LockReentrantError."""
    lock = reentrant_thread_lock

    def sync() -> None:
        lock.from_thread.acquire()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire()

    await asyncio.to_thread(sync)


async def test_lock_reentrant_from_thread_acquire_nowait(
    reentrant_thread_lock: Lock,
) -> None:
    """Test Lock nested from_thread acquire_nowait raises LockReentrantError."""
    lock = reentrant_thread_lock

    def sync() -> None:
        lock.from_thread.acquire_nowait()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire_nowait()

    await asyncio.to_thread(sync)


async def test_lock_reentrant_from_thread_acquire_then_acquire_nowait(
    reentrant_thread_lock: Lock,
) -> None:
    """Test Lock from_thread acquire then acquire_nowait raises LockReentrantError."""
    lock = reentrant_thread_lock

    def sync() -> None:
        lock.from_thread.acquire()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire_nowait()

    await asyncio.to_thread(sync)


async def test_lock_reentrant_from_thread_acquire_nowait_then_acquire(
    reentrant_thread_lock: Lock,
) -> None:
    """Test Lock from_thread acquire_nowait then acquire raises LockReentrantError."""
    lock = reentrant_thread_lock

    def sync() -> None:
        lock.from_thread.acquire_nowait()
        with pytest.raises(LockReentrantError):
            lock.from_thread.acquire()

    await asyncio.to_thread(sync)


async def test_lock_from_thread_reacquire_after_release(lock: Lock) -> None:
    """Test Lock from_thread can be acquired again after release."""

    def sync() -> None:
        lock.from_thread.acquire()
        lock.from_thread.release()
        lock.from_thread.acquire()
        assert lock.from_thread.locked() is True

    await asyncio.to_thread(sync)


async def test_lock_from_thread_reacquire_after_context_manager(
    lock: Lock,
) -> None:
    """Test Lock from_thread can be acquired again after context manager exit."""

    def sync() -> None:
        with lock.from_thread:
            pass
        with lock.from_thread:
            assert lock.from_thread.locked() is True

    await asyncio.to_thread(sync)


async def test_lock_acquire_cancelled_midflight(locks: list[Lock]) -> None:
    """A cancelled blocked acquire leaves the lock free for the next waiter."""
    # Arrange: worker 1 holds the lock so worker 2 blocks on acquire.
    await locks[WORKER_1].acquire()

    # Act: worker 2 starts acquiring, then gets cancelled mid-flight.
    task = await cancel_midflight(locks[WORKER_2].acquire, after=0)

    # Assert: the cancelled waiter never took ownership.
    assert task.cancelled() is True
    assert await locks[WORKER_2].owned() is False


async def test_lock_retry_interval_too_small(backend: LockBackend) -> None:
    """Test Lock rejects retry_interval below minimum."""
    with pytest.raises(ValueError, match="retry_interval must be"):
        Lock(name="test", backend=backend, retry_interval=0.0001)


# --- reconfigure ---


async def test_reconfigure_swaps_config(lock: Lock) -> None:
    """Reconfigure publishes the new config."""
    new_config = lock.config.model_copy(
        update={"lease_duration": 5, "retry_interval": 0.05},
    )

    await lock.reconfigure(new_config)

    assert lock.config == new_config


async def test_reconfigure_same_config_is_noop(lock: Lock) -> None:
    """Equal configs short-circuit."""
    same = lock.config.model_copy()

    await lock.reconfigure(same)

    assert lock.config == same


async def test_reconfigure_rejects_worker_change(lock: Lock) -> None:
    """Changing `worker` is not allowed because the token identity is live."""
    new_config = lock.config.model_copy(update={"worker": "other-worker"})

    with pytest.raises(ValueError, match="cannot change worker"):
        await lock.reconfigure(new_config)

    assert lock.config.worker != "other-worker"


async def test_reconfigure_changes_lease_duration_for_next_acquire(
    lock: Lock,
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """Acquire after reconfigure passes the new lease_duration to the backend."""
    spy = mocker.spy(backend, "acquire")
    new_config = lock.config.model_copy(update={"lease_duration": 42})

    await lock.reconfigure(new_config)
    await lock.acquire()
    await lock.release()

    assert spy.call_args.kwargs["duration"] == 42  # noqa: PLR2004


async def test_reconfigure_while_held_keeps_release_working(lock: Lock) -> None:
    """A swap during a held lease does not break the release path."""
    new_config = lock.config.model_copy(update={"lease_duration": 5})

    await lock.acquire()
    await lock.reconfigure(new_config)
    await lock.release()

    assert await lock.locked() is False


@pytest.mark.parametrize(
    "name",
    [
        "has space",
        "has\ttab",
        "control\x00char",
        "-leading-dash",
        "/leading-slash",
        ":leading-colon",
        "a" * 201,
    ],
)
def test_lock_rejects_unsafe_names(name: str) -> None:
    """Names with whitespace, control chars, or bad leaders are rejected."""
    with pytest.raises(ValueError, match="Invalid lock name") as exc:
        Lock(name)
    assert "Valid examples:" in str(exc.value)


@pytest.mark.parametrize(
    "name",
    [
        "cart",
        "users:42",
        "payments/eu",
        "weather.svc",
        "a-b_c.d:e/f",
    ],
)
def test_lock_accepts_safe_names(name: str) -> None:
    """Namespaced names with dots, dashes, slashes, and colons are accepted."""
    Lock(name)


# --- acquire with timeout ---


async def test_lock_acquire_timeout_raises_when_held(
    backend: LockBackend,
) -> None:
    """`acquire(timeout=...)` raises TimeoutError when the lock stays held."""
    # Use a long lease so the lock stays held for the full timeout window.
    holder = Lock(
        name=LOCK_NAME,
        backend=backend,
        worker="worker_holder",
        lease_duration=5.0,
        retry_interval=0.001,
    )
    waiter = Lock(
        name=LOCK_NAME,
        backend=backend,
        worker="worker_waiter",
        lease_duration=5.0,
        retry_interval=0.001,
    )
    await holder.acquire()

    with pytest.raises(TimeoutError):
        await waiter.acquire(timeout=0.01)


async def test_lock_acquire_timeout_succeeds_when_released_in_time(
    locks: list[Lock],
) -> None:
    """`acquire(timeout=...)` succeeds when the lock expires before the deadline.

    Uses lease expiry (lease_duration=0.01) so the same task does not need
    to release from a separate asyncio task (which would have a different
    token and fail the ownership check).
    """
    await locks[WORKER_1].acquire()
    # lease_duration=0.01, so the lock expires well before timeout=0.5
    handle = await locks[WORKER_2].acquire(timeout=0.5)

    assert isinstance(handle, LockHandle)
    assert handle.fencing_token >= 1


async def test_lock_acquire_timeout_none_waits_forever(
    locks: list[Lock],
) -> None:
    """`acquire(timeout=None)` waits until the lock expires (existing behavior)."""
    await locks[WORKER_1].acquire()
    # lease_duration=0.01, so the lock expires and WORKER_2 can acquire
    handle = await locks[WORKER_2].acquire(timeout=None)

    assert isinstance(handle, LockHandle)


# --- extend ---


async def test_lock_extend_renews_same_fencing_token(lock: Lock) -> None:
    """`extend()` renews the lease and returns the same fencing token."""
    first = await lock.acquire()

    extended = await lock.extend()

    assert extended.fencing_token == first.fencing_token
    assert extended.token == first.token


async def test_lock_extend_not_held_raises(lock: Lock) -> None:
    """`extend()` raises LockNotOwnedError when not holding the lock."""
    with pytest.raises(LockNotOwnedError):
        await lock.extend()


async def test_lock_extend_lease_lost_raises(
    locks: list[Lock], backend: LockBackend, mocker: MockerFixture
) -> None:
    """`extend()` raises LockNotOwnedError when the backend reports the lease is gone."""
    await locks[WORKER_1].acquire()
    mocker.patch.object(backend, "acquire", return_value=None)

    with pytest.raises(LockNotOwnedError):
        await locks[WORKER_1].extend()


# --- jitter ---


async def test_lock_acquire_jitter_applied(
    lock: Lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jitter scales the retry interval when _random is pinned."""
    monkeypatch.setattr(base_module, "_random", lambda: 1.0)
    # With jitter=0.1 and _random()=1.0: interval = retry_interval * (1 + 0.1 * 1.0) = 1.1x
    # This test just verifies acquire still completes without error.
    handle = await lock.acquire()
    assert handle.fencing_token >= 1


async def test_lock_acquire_jitter_zero_disables_jitter(
    backend: LockBackend,
) -> None:
    """When retry_jitter=0, no jitter multiplication occurs."""
    no_jitter_lock = Lock(
        name=LOCK_NAME,
        backend=backend,
        worker="worker_nojitter",
        lease_duration=0.01,
        retry_interval=0.001,
        retry_jitter=0.0,
    )
    handle = await no_jitter_lock.acquire()
    assert handle.fencing_token >= 1


async def test_acquire_without_jitter_uses_fixed_interval(
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """A zero `retry_jitter` retries on the fixed interval."""
    # Arrange: the first attempt is rejected, the second wins.
    expected_fencing_token = 7
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=0.01,
        retry_interval=0.001,
        retry_jitter=0,
    )
    mocker.patch.object(
        waiter,
        "do_acquire",
        mocker.AsyncMock(side_effect=[None, expected_fencing_token]),
    )

    # Act
    handle = await waiter.acquire()

    # Assert
    assert handle.fencing_token == expected_fencing_token


async def test_acquire_with_jitter_scales_retry_sleep(
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """A non-zero `retry_jitter` scales the sleep between retries."""
    # Arrange: the first attempt is rejected, the second wins.
    expected_fencing_token = 7
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=0.01,
        retry_interval=0.001,
        retry_jitter=0.5,
    )
    mocker.patch.object(
        waiter,
        "do_acquire",
        mocker.AsyncMock(side_effect=[None, expected_fencing_token]),
    )
    mocker.patch.object(base_module, "_random", return_value=1.0)

    # Act
    handle = await waiter.acquire()

    # Assert
    assert handle.fencing_token == expected_fencing_token


async def test_acquire_jitter_formula_exact_sleep(
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """The retry sleep equals retry_interval * (1 + jitter * (2*_random - 1))."""
    # Arrange: first attempt rejected, second wins, so exactly one sleep runs.
    retry_interval = 0.2
    jitter = 0.4
    random_value = 0.75
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=0.01,
        retry_interval=retry_interval,
        retry_jitter=jitter,
    )
    mocker.patch.object(
        waiter, "do_acquire", mocker.AsyncMock(side_effect=[None, 7])
    )
    mocker.patch.object(base_module, "_random", return_value=random_value)
    recorded: list[float] = []
    mocker.patch.object(
        lock_module.asyncio,
        "sleep",
        mocker.AsyncMock(side_effect=recorded.append),
    )

    # Act
    await waiter.acquire()

    # Assert
    expected = retry_interval * (1.0 + jitter * (2.0 * random_value - 1.0))
    assert recorded == [pytest.approx(expected)]


async def test_acquire_no_jitter_exact_sleep(
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """With retry_jitter=0 the retry sleep equals retry_interval exactly."""
    # Arrange: first attempt rejected, second wins, so exactly one sleep runs.
    retry_interval = 0.2
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=0.01,
        retry_interval=retry_interval,
        retry_jitter=0,
    )
    mocker.patch.object(
        waiter, "do_acquire", mocker.AsyncMock(side_effect=[None, 7])
    )
    recorded: list[float] = []
    mocker.patch.object(
        lock_module.asyncio,
        "sleep",
        mocker.AsyncMock(side_effect=recorded.append),
    )

    # Act
    await waiter.acquire()

    # Assert
    assert recorded == [pytest.approx(retry_interval)]


async def test_thread_acquire_jitter_formula_exact_sleep(
    backend: LockBackend,
    mocker: MockerFixture,
) -> None:
    """The thread acquire retry sleep applies the same jitter formula."""
    # Arrange: first attempt rejected, second wins, so exactly one sleep runs.
    retry_interval = 0.2
    jitter = 0.4
    random_value = 0.75
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=0.01,
        retry_interval=retry_interval,
        retry_jitter=jitter,
    )
    mocker.patch.object(
        waiter, "do_acquire", mocker.AsyncMock(side_effect=[None, 7])
    )
    mocker.patch.object(base_module, "_random", return_value=random_value)
    recorded: list[float] = []
    mocker.patch.object(
        lock_module.asyncio,
        "sleep",
        mocker.AsyncMock(side_effect=recorded.append),
    )

    # Act
    await waiter.do_thread_acquire(get_ident())

    # Assert
    expected = retry_interval * (1.0 + jitter * (2.0 * random_value - 1.0))
    assert recorded == [pytest.approx(expected)]


async def test_from_thread_acquire_timeout(backend: LockBackend) -> None:
    """A bounded thread acquire raises TimeoutError at the deadline."""
    # Arrange
    holder = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="holder",
        lease_duration=60,
        retry_interval=0.001,
    )
    waiter = Lock(
        backend=backend,
        name=LOCK_NAME,
        worker="waiter",
        lease_duration=60,
        retry_interval=0.001,
        retry_jitter=0,
    )
    await holder.acquire()

    # Act & Assert
    def sync() -> None:
        with pytest.raises(TimeoutError, match="not acquired"):
            waiter.from_thread.acquire(timeout=0.01)

    await asyncio.to_thread(sync)
    await holder.release()


async def test_from_thread_extend(lock: Lock) -> None:
    """A thread extend renews the lease and keeps the fencing token."""
    # Arrange
    acquired: LockHandle | None = None
    extended: LockHandle | None = None

    # Act
    def sync() -> None:
        nonlocal acquired, extended
        acquired = lock.from_thread.acquire()
        extended = lock.from_thread.extend()
        lock.from_thread.release()

    await asyncio.to_thread(sync)

    # Assert
    assert acquired is not None
    assert extended is not None
    assert extended.fencing_token == acquired.fencing_token


async def test_from_thread_extend_not_owned(lock: Lock) -> None:
    """A thread extend without holding the lock raises LockNotOwnedError."""

    # Act & Assert
    def sync() -> None:
        with pytest.raises(LockNotOwnedError):
            lock.from_thread.extend()

    await asyncio.to_thread(sync)


async def test_from_thread_extend_lost_lease(
    lock: Lock,
    mocker: MockerFixture,
) -> None:
    """A thread extend after the lease was lost raises LockNotOwnedError."""

    # Act & Assert: acquire and extend run on one thread, the backend
    # rejects the renewal as a lost lease.
    def sync() -> None:
        lock.from_thread.acquire()
        mocker.patch.object(
            lock, "do_acquire", mocker.AsyncMock(return_value=None)
        )
        with pytest.raises(LockNotOwnedError):
            lock.from_thread.extend()

    await asyncio.to_thread(sync)


async def test_lock_backend_out_of_context() -> None:
    """A `Lock` with no backend and no active app raises `OutOfContextError`."""
    with pytest.raises(OutOfContextError, match="Lock\\('out-of-context'\\)"):
        _ = Lock("out-of-context").backend


# --- error name in message ---


async def test_reentrant_error_carries_name(lock: Lock) -> None:
    """`LockReentrantError` names the lock in its message."""
    await lock.acquire()
    with pytest.raises(LockReentrantError) as exc:
        await lock.acquire()
    assert f"name={LOCK_NAME}" in str(exc.value)


async def test_not_owned_error_carries_name(lock: Lock) -> None:
    """`LockNotOwnedError` names the lock in its message."""
    with pytest.raises(LockNotOwnedError) as exc:
        await lock.release()
    assert f"name={LOCK_NAME}" in str(exc.value)


async def test_acquire_error_carries_name(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """`LockAcquireError` names the lock in its message."""
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Error")
    )
    with pytest.raises(LockAcquireError) as exc:
        await lock.acquire()
    assert f"name={LOCK_NAME}" in str(exc.value)


async def test_release_error_carries_name(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """`LockReleaseError` names the lock in its message."""
    await lock.acquire()
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Error")
    )
    with pytest.raises(LockReleaseError) as exc:
        await lock.release()
    assert f"name={LOCK_NAME}" in str(exc.value)


async def test_locked_check_error_carries_name(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """`LockLockedCheckError` names the lock in its message."""
    mocker.patch.object(
        backend, "locked", side_effect=Exception("Backend Error")
    )
    with pytest.raises(LockLockedCheckError) as exc:
        await lock.locked()
    assert f"name={LOCK_NAME}" in str(exc.value)


async def test_owned_check_error_carries_name(
    backend: LockBackend, lock: Lock, mocker: MockerFixture
) -> None:
    """`LockOwnedCheckError` names the lock in its message."""
    mocker.patch.object(
        backend, "owned", side_effect=Exception("Backend Error")
    )
    with pytest.raises(LockOwnedCheckError) as exc:
        await lock.owned()
    assert f"name={LOCK_NAME}" in str(exc.value)
