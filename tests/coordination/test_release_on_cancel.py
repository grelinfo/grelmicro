"""A release that has started finishes on the backend, even when its task is cancelled.

A request whose client disconnects, or a worker shutting down, cancels the
task leaving `async with lock:`. If that cancel abandoned the backend call,
the lease would stay held until it expires and every other holder would wait
for it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grelmicro.coordination.errors import LockReleaseError
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import (
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
)
from grelmicro.coordination.readwritelock import ReadWriteLock
from grelmicro.coordination.tasklock import TaskLock

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Awaitable,
        Callable,
        Coroutine,
    )

    from pytest_mock import MockerFixture

pytestmark = [pytest.mark.timeout(5)]


@pytest.fixture
async def lock_backend() -> AsyncGenerator[MemoryLockAdapter]:
    """Return an open in-process lock backend."""
    async with MemoryLockAdapter() as backend:
        yield backend


@pytest.fixture
async def rw_backend() -> AsyncGenerator[MemoryReadWriteLockAdapter]:
    """Return an open in-process read-write lock backend."""
    async with MemoryReadWriteLockAdapter() as backend:
        yield backend


def _pause(
    mocker: MockerFixture, backend: object, method: str
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold `backend.method` until the returned `resume` event is set.

    Returns `(started, resume)`: `started` is set once the call reached
    the backend.
    """
    started = asyncio.Event()
    resume = asyncio.Event()
    original: Callable[..., Awaitable[object]] = getattr(backend, method)

    async def paused(**kwargs: object) -> object:
        started.set()
        await resume.wait()
        return await original(**kwargs)

    mocker.patch.object(backend, method, side_effect=paused)
    return started, resume


async def _cancel_while_releasing(
    body: Callable[[], Coroutine[object, object, None]],
    started: asyncio.Event,
    resume: asyncio.Event,
) -> None:
    """Run `body`, cancel it once its release reached the backend, then resume it."""
    task = asyncio.create_task(body())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_lock_release_finishes_when_cancelled(
    lock_backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving `async with lock:` under a cancel still frees the lock."""
    lock = Lock("orders", backend=lock_backend)
    started, resume = _pause(mocker, lock_backend, "release")

    async def body() -> None:
        async with lock:
            pass

    await _cancel_while_releasing(body, started, resume)

    assert not await lock.locked()


async def test_read_release_finishes_when_cancelled(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving `async with lock.read:` under a cancel still frees the read lease."""
    lock = ReadWriteLock("catalog", backend=rw_backend)
    started, resume = _pause(mocker, rw_backend, "release_read")

    async def body() -> None:
        async with lock.read:
            pass

    await _cancel_while_releasing(body, started, resume)

    async with asyncio.timeout(1), lock.write:
        pass


async def test_write_release_finishes_when_cancelled(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving `async with lock.write:` under a cancel still frees the write lease."""
    lock = ReadWriteLock("catalog", backend=rw_backend)
    started, resume = _pause(mocker, rw_backend, "release_write")

    async def body() -> None:
        async with lock.write:
            pass

    await _cancel_while_releasing(body, started, resume)

    async with asyncio.timeout(1), lock.read:
        pass


async def test_task_lock_release_finishes_when_cancelled(
    lock_backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving `async with task_lock:` under a cancel still frees the claim."""
    task_lock = TaskLock(
        "report",
        backend=lock_backend,
        min_hold_duration=timedelta(milliseconds=1),
    )
    started, resume = _pause(mocker, lock_backend, "release")

    async def body() -> None:
        async with task_lock:
            await asyncio.sleep(0.01)

    await _cancel_while_releasing(body, started, resume)

    assert not await task_lock.locked()


async def test_lock_release_is_bounded_by_the_lease(
    lock_backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """A release the backend never answers gives up once the lease is over."""
    lock = Lock(
        "orders",
        backend=lock_backend,
        lease_duration=timedelta(milliseconds=50),
    )
    await lock.acquire()
    never = asyncio.Event()

    async def hang(**_: object) -> bool:
        await never.wait()
        return True

    mocker.patch.object(lock_backend, "release", side_effect=hang)

    with pytest.raises(LockReleaseError):
        await lock.release()


async def test_lock_release_keeps_a_cancel_outside_the_release(
    lock_backend: MemoryLockAdapter,
) -> None:
    """A cancel that lands after the release still cancels the task."""
    lock = Lock("orders", backend=lock_backend)

    async def body() -> None:
        async with lock:
            pass
        await asyncio.Event().wait()

    task = asyncio.create_task(body())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await lock.locked()


async def test_write_intent_is_withdrawn_when_cancelled_twice(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """A writer cancelled while it waits still withdraws its intent, even if cancelled again."""
    lock = ReadWriteLock("catalog", backend=rw_backend, retry_interval=0.001)
    started, resume = _pause(mocker, rw_backend, "cancel_intent")

    async def write() -> None:
        async with lock.write:
            pass

    async with lock.read:
        task = asyncio.create_task(write())
        await asyncio.sleep(0.01)
        task.cancel()
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    async with asyncio.timeout(1), lock.read:
        pass


async def test_read_lease_is_forgotten_when_cancelled_mid_release(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """A task that survives a cancel mid-release holds no read lease after it."""
    lock = ReadWriteLock("catalog", backend=rw_backend)
    started, resume = _pause(mocker, rw_backend, "release_read")
    owned: list[bool] = []

    async def body() -> None:
        try:
            async with lock.read:
                pass
        except asyncio.CancelledError:
            owned.append(await lock.read.owned())
            raise

    await _cancel_while_releasing(body, started, resume)

    assert owned == [False]


async def test_lock_release_past_the_lease_stops_holding(
    lock_backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """A release that ran past the lease no longer counts the task as holder."""
    lock = Lock(
        "orders",
        backend=lock_backend,
        lease_duration=timedelta(milliseconds=50),
    )
    await lock.acquire()
    never = asyncio.Event()

    async def hang(**_: object) -> bool:
        await never.wait()
        return True

    mocker.patch.object(lock_backend, "release", side_effect=hang)

    with pytest.raises(LockReleaseError):
        await lock.release()

    mocker.stopall()
    await lock.acquire()


async def test_read_release_past_the_lease_stops_holding(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """A read release that ran past the lease leaves the task holding nothing."""
    lock = ReadWriteLock(
        "catalog", backend=rw_backend, lease_duration=timedelta(milliseconds=50)
    )
    await lock.read.acquire()
    never = asyncio.Event()

    async def hang(**_: object) -> bool:
        await never.wait()
        return True

    mocker.patch.object(rw_backend, "release_read", side_effect=hang)

    with pytest.raises(LockReleaseError):
        await lock.read.release()

    mocker.stopall()
    await lock.read.acquire()


async def test_task_lock_shortened_hold_finishes_when_cancelled(
    lock_backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving early under a cancel still shortens the claim to the minimum hold."""
    task_lock = TaskLock(
        "report",
        backend=lock_backend,
        min_hold_duration=timedelta(milliseconds=200),
        lease_duration=60,
    )
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def body() -> None:
        async with task_lock:
            entered.set()
            await leave.wait()

    task = asyncio.create_task(body())
    await entered.wait()
    started, resume = _pause(mocker, lock_backend, "acquire")
    leave.set()
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)

    assert not await task_lock.locked()


async def test_write_release_past_the_lease_stops_holding(
    rw_backend: MemoryReadWriteLockAdapter, mocker: MockerFixture
) -> None:
    """A write release that ran past the lease leaves the task holding nothing."""
    lock = ReadWriteLock(
        "catalog", backend=rw_backend, lease_duration=timedelta(milliseconds=50)
    )
    await lock.write.acquire()
    never = asyncio.Event()

    async def hang(**_: object) -> bool:
        await never.wait()
        return True

    mocker.patch.object(rw_backend, "release_write", side_effect=hang)

    with pytest.raises(LockReleaseError):
        await lock.write.release()

    mocker.stopall()
    await lock.write.acquire()
