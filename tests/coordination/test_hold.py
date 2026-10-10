"""`hold(timeout=)`: wait at most `timeout` seconds, then keep the lock for the body."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import (
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
)
from grelmicro.coordination.readwritelock import ReadWriteLock
from grelmicro.errors import LockTimeoutError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.timeout(5)]

WAIT = 0.05
"""Seconds a bounded wait gives a held lock before giving up."""


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


@pytest.fixture
def rw_lock(rw_backend: MemoryReadWriteLockAdapter) -> ReadWriteLock:
    """Return a read-write lock on the in-process backend."""
    return ReadWriteLock("catalog", backend=rw_backend, retry_interval=0.001)


def _locks(backend: MemoryLockAdapter) -> tuple[Lock, Lock]:
    """Return the same lock as two workers see it."""
    first, second = (
        Lock(
            "orders",
            backend=backend,
            worker=f"worker_{i}",
            retry_interval=0.001,
        )
        for i in range(2)
    )
    return first, second


async def _read(lock: ReadWriteLock, wait: float) -> None:
    async with lock.read.hold(timeout=wait):
        pass


async def _write(lock: ReadWriteLock, wait: float) -> None:
    async with lock.write.hold(timeout=wait):
        pass


async def test_lock_hold_free_lock_runs_the_body_and_releases(
    lock_backend: MemoryLockAdapter,
) -> None:
    """The body runs under the lock, and leaving it releases the lock."""
    # Arrange
    lock, _ = _locks(lock_backend)

    # Act
    async with lock.hold(timeout=WAIT) as held:
        locked_inside = await lock.locked()

    # Assert
    assert locked_inside
    assert held.fencing_token >= 1
    assert not await lock.locked()


@pytest.mark.parametrize("wait", [WAIT, 0])
async def test_lock_hold_held_lock_raises_and_skips_the_body(
    lock_backend: MemoryLockAdapter, wait: float
) -> None:
    """A lock someone else holds raises once the wait runs out, `0` after one try."""
    # Arrange
    mine, theirs = _locks(lock_backend)
    ran = False

    # Act / Assert
    async with theirs:
        with pytest.raises(LockTimeoutError):
            async with mine.hold(timeout=wait):
                ran = True

    assert not ran


async def test_lock_hold_body_error_releases_the_lock(
    lock_backend: MemoryLockAdapter,
) -> None:
    """An error in the body still releases the lock."""
    # Arrange
    lock, _ = _locks(lock_backend)
    msg = "body"

    # Act
    with pytest.raises(ValueError, match="body"):
        async with lock.hold(timeout=WAIT):
            raise ValueError(msg)

    # Assert
    assert not await lock.locked()


async def test_lock_hold_awaited_raises_type_error(
    lock_backend: MemoryLockAdapter,
) -> None:
    """Awaiting `hold()` fails loudly instead of acquiring nothing."""
    # Arrange
    lock, _ = _locks(lock_backend)

    # Act / Assert
    with pytest.raises(TypeError):
        await lock.hold(timeout=WAIT)  # type: ignore[misc] # ty: ignore[invalid-await]


async def test_lock_hold_sync_with_raises_type_error(
    lock_backend: MemoryLockAdapter,
) -> None:
    """A sync `with` on the async form fails loudly."""
    # Arrange
    lock, _ = _locks(lock_backend)

    # Act / Assert
    with pytest.raises(TypeError), lock.hold(timeout=WAIT):  # type: ignore[attr-defined] # ty: ignore[invalid-context-manager]
        pass


@pytest.mark.parametrize("wait", [WAIT, 0])
async def test_read_hold_under_a_writer_raises(
    rw_lock: ReadWriteLock, wait: float
) -> None:
    """A reader gives up while a writer holds the lock."""
    # Act / Assert
    async with rw_lock.write:
        with pytest.raises(LockTimeoutError):
            await asyncio.create_task(_read(rw_lock, wait))


async def test_read_hold_free_lock_yields_a_valid_guard(
    rw_lock: ReadWriteLock,
) -> None:
    """A free lock grants the read lease for the body."""
    # Act
    async with rw_lock.read.hold(timeout=WAIT) as reading:
        valid_inside = reading.valid

    # Assert
    assert valid_inside
    assert not reading.valid


@pytest.mark.parametrize("wait", [WAIT, 0])
async def test_write_hold_under_a_reader_raises(
    rw_lock: ReadWriteLock, wait: float
) -> None:
    """A writer gives up while a reader holds the lock."""
    # Act / Assert
    async with rw_lock.read:
        with pytest.raises(LockTimeoutError):
            await asyncio.create_task(_write(rw_lock, wait))


async def test_write_hold_downgrade_releases_the_read_lease(
    rw_lock: ReadWriteLock,
) -> None:
    """Leaving after a downgrade releases the read lease, like `async with lock.write:`."""
    # Act
    async with rw_lock.write.hold(timeout=WAIT) as writing:
        await writing.downgrade()

    # Assert
    async with rw_lock.write.hold(timeout=WAIT) as again:
        assert again.valid


async def test_lock_from_thread_hold_free_lock_runs_the_body(
    lock_backend: MemoryLockAdapter,
) -> None:
    """A worker thread holds the lock for its `with` body."""
    # Arrange
    lock, _ = _locks(lock_backend)

    def work() -> tuple[bool, int]:
        with lock.from_thread.hold(timeout=WAIT) as held:
            return lock.from_thread.locked(), held.fencing_token

    # Act
    locked, token = await asyncio.to_thread(work)

    # Assert
    assert locked
    assert token >= 1
    assert not await lock.locked()


@pytest.mark.parametrize("wait", [WAIT, 0])
async def test_lock_from_thread_hold_held_lock_raises(
    lock_backend: MemoryLockAdapter, wait: float
) -> None:
    """A worker thread gives up on a lock someone else holds."""
    # Arrange
    mine, theirs = _locks(lock_backend)

    def blocked() -> None:
        with theirs.from_thread.hold(timeout=wait):
            pass

    # Act / Assert
    async with mine:
        with pytest.raises(LockTimeoutError):
            await asyncio.to_thread(blocked)


async def test_lock_from_thread_hold_async_with_raises_type_error(
    lock_backend: MemoryLockAdapter,
) -> None:
    """An `async with` on the thread form fails loudly."""
    # Arrange
    lock, _ = _locks(lock_backend)

    # Act / Assert
    with pytest.raises(TypeError):
        async with lock.from_thread.hold(timeout=WAIT):  # type: ignore[attr-defined] # ty: ignore[invalid-context-manager]
            pass


async def test_rw_from_thread_hold_free_lock_grants_both_sides(
    rw_lock: ReadWriteLock,
) -> None:
    """Both sides of a read-write lock hold from a worker thread."""

    # Arrange
    def read_then_write() -> tuple[bool, bool]:
        with rw_lock.read.from_thread.hold(timeout=WAIT) as reading:
            read_valid = reading.valid
        with rw_lock.write.from_thread.hold(timeout=WAIT) as writing:
            return read_valid, writing.valid

    # Act
    read_valid, write_valid = await asyncio.to_thread(read_then_write)

    # Assert
    assert read_valid
    assert write_valid


@pytest.mark.parametrize("wait", [WAIT, 0])
async def test_rw_from_thread_hold_under_a_writer_raises(
    rw_lock: ReadWriteLock, wait: float
) -> None:
    """A reader thread gives up while a writer holds the lock."""

    # Arrange
    def read() -> None:
        with rw_lock.read.from_thread.hold(timeout=wait):
            pass

    # Act / Assert
    async with rw_lock.write:
        with pytest.raises(LockTimeoutError):
            await asyncio.to_thread(read)
