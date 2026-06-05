"""Tests for grelmicro._async utilities."""

import asyncio
import functools

from grelmicro._async import is_async_callable


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
