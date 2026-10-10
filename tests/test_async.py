"""Tests for grelmicro._async utilities."""

import asyncio
import functools

import pytest

from grelmicro._async import is_async_callable, run_to_completion

ANSWER = 42


async def _async_fn() -> None:
    """Plain async function."""


def _sync_fn() -> None:
    """Plain sync function."""


class _AsyncCallable:
    async def __call__(self) -> None:
        """Instance with async __call__."""


class _SyncCallable:
    def __call__(self) -> None:
        """Instance with sync __call__."""


def test_plain_async_function() -> None:
    """A plain async def is detected as async."""
    assert is_async_callable(_async_fn) is True


def test_plain_sync_function() -> None:
    """A plain def is detected as sync."""
    assert is_async_callable(_sync_fn) is False


def test_async_callable_class() -> None:
    """An instance with ``async def __call__`` is async."""
    assert is_async_callable(_AsyncCallable()) is True


def test_sync_callable_class() -> None:
    """An instance with plain ``__call__`` is sync."""
    assert is_async_callable(_SyncCallable()) is False


def test_partial_of_async_is_async() -> None:
    """``functools.partial(async_fn)`` is detected as async."""
    assert is_async_callable(functools.partial(_async_fn)) is True


def test_nested_partial_of_async_is_async() -> None:
    """Nested partials unwrap recursively."""
    assert (
        is_async_callable(functools.partial(functools.partial(_async_fn)))
        is True
    )


def test_partial_of_sync_is_sync() -> None:
    """``functools.partial`` of a sync function stays sync."""
    assert is_async_callable(functools.partial(_sync_fn)) is False


async def test_sleep_or_stop_returns_false_on_timeout() -> None:
    """With no stop set, the full interval elapses and returns False."""
    from grelmicro._async import sleep_or_stop  # noqa: PLC0415

    assert await sleep_or_stop(0.01, asyncio.Event()) is False


async def test_sleep_or_stop_returns_true_when_already_set() -> None:
    """A stop already set returns True without sleeping."""
    from grelmicro._async import sleep_or_stop  # noqa: PLC0415

    stop = asyncio.Event()
    stop.set()
    assert await sleep_or_stop(60, stop) is True


async def test_sleep_or_stop_wakes_on_stop_during_wait() -> None:
    """A stop set during the wait wakes early and returns True."""
    from grelmicro._async import sleep_or_stop  # noqa: PLC0415

    stop = asyncio.Event()

    async def trip() -> None:
        await asyncio.sleep(0.01)
        stop.set()

    async with asyncio.timeout(2):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(trip())
            assert await sleep_or_stop(60, stop) is True


async def test_sleep_or_stop_none_is_plain_sleep() -> None:
    """A None stop behaves like asyncio.sleep and returns False."""
    from grelmicro._async import sleep_or_stop  # noqa: PLC0415

    assert await sleep_or_stop(0.01, None) is False


async def test_run_to_completion_returns_the_result() -> None:
    """Uncancelled, the work's result comes back."""

    async def work() -> int:
        return ANSWER

    assert await run_to_completion(work()) == ANSWER


async def test_run_to_completion_raises_the_work_error() -> None:
    """Uncancelled, the work's error is raised."""

    async def work() -> None:
        msg = "work failed"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="work failed"):
        await run_to_completion(work())


async def test_run_to_completion_finishes_before_the_cancel() -> None:
    """Cancelled twice, the caller still waits for the work, then is cancelled."""
    resume = asyncio.Event()
    done: list[bool] = []

    async def work() -> None:
        await resume.wait()
        done.append(True)

    task = asyncio.create_task(run_to_completion(work()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]


async def test_run_to_completion_keeps_an_outer_timeout() -> None:
    """An enclosing `asyncio.timeout` still reports its timeout once the work ends."""

    async def work() -> None:
        await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await run_to_completion(work())


async def test_run_to_completion_keeps_the_cancel_over_a_work_error() -> None:
    """A cancel the caller received wins over an error the work raises after it."""
    resume = asyncio.Event()

    async def work() -> None:
        await resume.wait()
        msg = "work failed"
        raise ValueError(msg)

    task = asyncio.create_task(run_to_completion(work()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
