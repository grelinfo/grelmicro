"""`hold(auto_extend=True)`: keep the lease while the body runs, cancel it on loss."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grelmicro.coordination._hold import Hold
from grelmicro.coordination.errors import (
    LockExtendError,
    LockNotOwnedError,
)
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import WouldBlockError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pytest_mock import MockerFixture

pytestmark = [pytest.mark.timeout(10)]

LEASE = 0.15
"""Seconds of lease, short enough to outlive several of them in a test."""

WAIT = 0.5
"""Seconds a hold waits for the lock."""

RENEWALS = 2
"""Renewals a body of two leases sees at least."""

RETRIES = 3
"""Tries a failing backend gets at least before the lease is given up."""


@pytest.fixture
async def backend() -> AsyncGenerator[MemoryLockAdapter]:
    """Return an open in-process lock backend."""
    async with MemoryLockAdapter() as backend:
        yield backend


def _lock(
    backend: MemoryLockAdapter, worker: str = "worker_0", lease: float = LEASE
) -> Lock:
    """Return the `orders` lock as `worker` sees it, with a short lease."""
    return Lock(
        "orders",
        backend=backend,
        worker=worker,
        lease_duration=timedelta(seconds=lease),
        retry_interval=0.001,
    )


def _extend_with(
    lock: Lock,
    mocker: MockerFixture,
    answer: Callable[[str, timedelta], Awaitable[int | None]],
) -> None:
    """Route the lock's backend extensions through `answer`."""
    mocker.patch.object(lock, "_backend_extend", side_effect=answer)


def _current() -> asyncio.Task[object]:
    """Return the running task."""
    task = asyncio.current_task()
    assert task is not None
    return task


def _other_tasks() -> set[asyncio.Task[object]]:
    """Return every task on the loop but the running one."""
    return asyncio.all_tasks() - {_current()}


async def test_auto_extend_keeps_the_lock_past_several_leases(
    backend: MemoryLockAdapter,
) -> None:
    """The body outlives four leases and the lock is never free for another worker."""
    # Arrange
    lock = _lock(backend)
    other = _lock(backend, "worker_1")

    # Act
    async with lock.hold(timeout=WAIT, auto_extend=True) as held:
        await asyncio.sleep(LEASE * 4)
        with pytest.raises(WouldBlockError):
            await other.acquire_nowait()
        owned = await backend.owned(name="lock:orders", token=held.token)

    # Assert
    assert owned
    assert not await lock.locked()


async def test_auto_extend_keeps_the_fencing_token(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """Each renewal keeps the fencing token the hold returned."""
    # Arrange
    lock = _lock(backend)
    extend = mocker.spy(lock, "_backend_extend")

    # Act
    async with lock.hold(timeout=WAIT, auto_extend=True) as held:
        await asyncio.sleep(LEASE * 2)

    # Assert
    assert extend.call_count >= RENEWALS
    assert {call.args[0] for call in extend.await_args_list} == {held.token}
    assert extend.spy_return == held.fencing_token


async def test_auto_extend_stops_renewing_once_the_body_ends(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """Leaving the body stops the renewer, leaves no task behind, and releases."""
    # Arrange
    lock = _lock(backend)
    extend = mocker.spy(lock, "_backend_extend")
    before = _other_tasks()

    # Act
    async with lock.hold(timeout=WAIT, auto_extend=True):
        await asyncio.sleep(LEASE)
        renewing = _other_tasks() - before
    calls = extend.call_count
    await asyncio.sleep(LEASE)

    # Assert
    assert len(renewing) == 1
    assert _other_tasks() - before == set()
    assert extend.call_count == calls
    assert not await lock.locked()


async def test_auto_extend_off_starts_no_renewer(
    backend: MemoryLockAdapter,
) -> None:
    """The default hold is the plain one and runs no task of its own."""
    # Arrange
    lock = _lock(backend)
    before = _other_tasks()

    # Act
    hold = lock.hold(timeout=WAIT)
    async with hold:
        await asyncio.sleep(0)
        during = _other_tasks() - before

    # Assert
    assert type(hold) is Hold
    assert during == set()


async def test_auto_extend_lost_lease_cancels_the_body_and_raises(
    backend: MemoryLockAdapter,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A renewal the backend refuses cancels the body at its next await."""
    # Arrange
    caplog.set_level("WARNING")
    lock = _lock(backend)

    async def refuse(_token: str, _duration: timedelta) -> None:
        return None

    _extend_with(lock, mocker, refuse)
    reached = False

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            await asyncio.sleep(LEASE * 10)
            reached = True

    # Assert
    assert not reached
    lost = [r for r in caplog.records if "orders" in r.getMessage()]
    assert len(lost) == 1
    assert lost[0].levelname == "WARNING"
    assert _current().cancelling() == 0
    await asyncio.sleep(0)
    held = await lock.acquire_nowait()
    await lock.release()
    assert held.fencing_token >= 1


async def test_auto_extend_takeover_cancels_the_body_and_raises(
    backend: MemoryLockAdapter,
) -> None:
    """Another worker taking the lock over ends the body with `LockNotOwnedError`."""
    # Arrange
    lock = _lock(backend)
    other = _lock(backend, "worker_1")
    reached = False

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True) as held:
            await backend.release(name="lock:orders", token=held.token)
            taken = await other.acquire_nowait()
            await asyncio.sleep(LEASE * 10)
            reached = True

    # Assert
    assert not reached
    assert taken.fencing_token > held.fencing_token
    assert await other.owned()
    await other.release()


@pytest.mark.parametrize("failure", ["error", "hang"])
async def test_auto_extend_gives_up_before_the_lease_runs_out(
    backend: MemoryLockAdapter,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """A backend that keeps failing is retried, then the body is cancelled in time."""
    # Arrange
    caplog.set_level("WARNING")
    lease = 2.0
    lock = _lock(backend, lease=lease)
    calls = 0

    async def fail(_token: str, _duration: timedelta) -> int | None:
        nonlocal calls
        calls += 1
        if failure == "hang":
            await asyncio.Event().wait()
        raise LockExtendError(name="orders")

    _extend_with(lock, mocker, fail)
    loop = asyncio.get_running_loop()
    entered_at = cancelled_at = 0.0

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            entered_at = loop.time()
            try:
                await asyncio.sleep(lease * 10)
            finally:
                cancelled_at = loop.time()

    # Assert
    assert cancelled_at - entered_at < lease
    assert calls == 1 if failure == "hang" else calls >= RETRIES
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1
    assert "orders" in warned[0].getMessage()


@pytest.mark.parametrize("failure", ["error", "hang", "lost"])
async def test_auto_extend_cancels_the_body_before_it_warns(
    backend: MemoryLockAdapter,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """A lost lease reaches the body before its warning is written."""
    # Arrange
    caplog.set_level("WARNING")
    lock = _lock(backend)

    async def fail(_token: str, _duration: timedelta) -> int | None:
        if failure == "hang":
            await asyncio.Event().wait()
        if failure == "error":
            raise LockExtendError(name="orders")
        return None

    _extend_with(lock, mocker, fail)
    warned_before_cancel = True

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            try:
                await asyncio.sleep(LEASE * 10)
            finally:
                warned_before_cancel = bool(caplog.records)

    # Assert
    assert not warned_before_cancel
    [warned] = caplog.records
    assert "orders" in warned.getMessage()
    assert (warned.exc_info is not None) is (failure != "lost")


async def test_auto_extend_transient_error_keeps_the_lock(
    backend: MemoryLockAdapter,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A renewal that fails once and then works keeps the body running."""
    # Arrange
    caplog.set_level("WARNING")
    lock = _lock(backend)
    other = _lock(backend, "worker_1")
    real = lock._backend_extend
    calls = 0

    async def flaky(token: str, duration: timedelta) -> int | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LockExtendError(name="orders")
        return await real(token, duration)

    _extend_with(lock, mocker, flaky)

    # Act
    async with lock.hold(timeout=WAIT, auto_extend=True):
        await asyncio.sleep(LEASE * 3)
        with pytest.raises(WouldBlockError):
            await other.acquire_nowait()

    # Assert
    assert calls >= RETRIES
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert not await lock.locked()


async def test_auto_extend_outer_cancellation_propagates(
    backend: MemoryLockAdapter,
) -> None:
    """Cancelling the holder raises `CancelledError` and releases the lock."""
    # Arrange
    lock = _lock(backend)
    entered = asyncio.Event()

    async def hold() -> None:
        async with lock.hold(timeout=WAIT, auto_extend=True):
            entered.set()
            await asyncio.sleep(LEASE * 10)

    task = asyncio.create_task(hold())
    await entered.wait()

    # Act
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Assert
    assert not await lock.locked()


async def test_auto_extend_outer_cancellation_wins_over_a_lost_lease(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """A lost lease never swallows a cancellation of the holder from outside."""
    # Arrange
    lock = _lock(backend)
    holder: asyncio.Task[None] | None = None

    async def refuse_and_cancel(_token: str, _duration: timedelta) -> None:
        assert holder is not None
        holder.cancel()

    _extend_with(lock, mocker, refuse_and_cancel)

    async def hold() -> None:
        async with lock.hold(timeout=WAIT, auto_extend=True):
            await asyncio.sleep(LEASE * 10)

    holder = asyncio.create_task(hold())

    # Act / Assert
    with pytest.raises(asyncio.CancelledError):
        await holder
    assert not await lock.locked()


async def test_auto_extend_body_swallowing_the_cancel_still_raises(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """A body that swallows the cancellation ends with `LockNotOwnedError`."""
    # Arrange
    lock = _lock(backend)

    async def refuse(_token: str, _duration: timedelta) -> None:
        return None

    _extend_with(lock, mocker, refuse)
    caught = False

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            try:
                await asyncio.sleep(LEASE * 10)
            except asyncio.CancelledError:
                caught = True

    # Assert
    assert caught
    assert _current().cancelling() == 0
    await asyncio.sleep(0)


async def test_auto_extend_body_error_after_loss_propagates(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """An error the body raises after the loss is the one that propagates."""
    # Arrange
    lock = _lock(backend)

    async def refuse(_token: str, _duration: timedelta) -> None:
        return None

    _extend_with(lock, mocker, refuse)
    msg = "body"

    # Act / Assert
    with pytest.raises(ValueError, match="body"):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            try:
                await asyncio.sleep(LEASE * 10)
            except asyncio.CancelledError:
                raise ValueError(msg) from None


async def test_auto_extend_loss_found_while_leaving_raises(
    backend: MemoryLockAdapter, mocker: MockerFixture
) -> None:
    """A renewal in flight when the body ends, then refused, raises on exit."""
    # Arrange
    lock = _lock(backend)
    sent = asyncio.Event()
    answer = asyncio.Event()

    async def refuse_later(_token: str, _duration: timedelta) -> None:
        sent.set()
        await answer.wait()

    _extend_with(lock, mocker, refuse_later)
    loop = asyncio.get_running_loop()

    # Act
    with pytest.raises(LockNotOwnedError):  # noqa: PT012
        async with lock.hold(timeout=WAIT, auto_extend=True):
            await sent.wait()
            loop.call_later(0.01, answer.set)

    # Assert
    assert _current().cancelling() == 0
    assert not await lock.locked()
