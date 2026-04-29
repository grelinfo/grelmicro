"""Regression tests: Lock.release clears local state only after backend confirms."""

from collections.abc import AsyncGenerator

import pytest
from anyio import get_current_task
from pytest_mock import MockerFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import (
    LockNotOwnedError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.sync.lock import Lock
from grelmicro.sync.memory import MemorySyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

LOCK_NAME = "test_lock_release_ordering"
WORKER = "worker_1"


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return a Memory sync backend."""
    async with MemorySyncBackend() as backend:
        yield backend


@pytest.fixture
def lock(backend: SyncBackend) -> Lock:
    """Return a Lock bound to the memory backend."""
    return Lock(LOCK_NAME, backend=backend, worker=WORKER, lease_duration=10)


# --- Async release ---


async def test_release_clears_state_on_success(lock: Lock) -> None:
    """Successful release clears the held marker."""
    await lock.acquire()
    await lock.release()

    # The held marker is gone, a second acquire would not be reentrant.
    await lock.acquire()
    await lock.release()


async def test_release_keeps_state_on_backend_error(
    lock: Lock,
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """A backend error during release keeps the held marker intact."""
    await lock.acquire()
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Unreachable")
    )

    with pytest.raises(LockReleaseError):
        await lock.release()

    # The held marker survives the failed backend release. A fresh
    # acquire from the same task hits the reentrant guard, proving
    # the marker is still set.
    assert get_current_task().id in lock._held_by_tasks
    with pytest.raises(LockReentrantError):
        await lock.acquire()


async def test_release_clears_state_when_backend_reports_not_owned(
    lock: Lock,
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """A "not owned" answer from the backend clears the held marker."""
    await lock.acquire()
    mocker.patch.object(backend, "release", return_value=False)

    with pytest.raises(LockNotOwnedError):
        await lock.release()

    # The backend authoritatively said we don't own it; local state
    # should reflect that, so the held marker is gone.
    assert get_current_task().id not in lock._held_by_tasks


# --- Thread release (do_thread_release) ---


async def test_thread_release_keeps_state_on_backend_error(
    lock: Lock,
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """A backend error during thread release keeps the held-by-thread marker intact."""
    thread_id = 42
    await lock.do_thread_acquire(thread_id)
    assert thread_id in lock._held_by_threads

    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Unreachable")
    )

    with pytest.raises(LockReleaseError):
        await lock.do_thread_release(thread_id)

    assert thread_id in lock._held_by_threads


async def test_thread_release_clears_state_when_not_owned(
    lock: Lock,
    backend: SyncBackend,
    mocker: MockerFixture,
) -> None:
    """A "not owned" answer from the backend clears the held-by-thread marker."""
    thread_id = 42
    await lock.do_thread_acquire(thread_id)
    mocker.patch.object(backend, "release", return_value=False)

    with pytest.raises(LockNotOwnedError):
        await lock.do_thread_release(thread_id)

    assert thread_id not in lock._held_by_threads
