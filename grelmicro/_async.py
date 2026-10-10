"""Shared async utilities."""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import TYPE_CHECKING, Any, NoReturn, TypeGuard

from grelmicro.errors import EventLoopDeadlockError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

_finishing: set[asyncio.Task[Any]] = set()
"""Tasks `run_to_completion` is waiting on, kept alive until they end."""


def raise_backend_not_open(what: str) -> NoReturn:
    """Raise for a sync adapter used before its backend captured a loop.

    Call this only on the failure branch of ``if loop is None``. ``what``
    names the caller, for example ``"Lock 'orders'"``.

    Raises:
        RuntimeError: Always.
    """
    msg = (
        f"{what} cannot be used from a worker thread before its backend "
        "is opened. Wrap startup with `async with micro:` or "
        "`async with backend:`."
    )
    raise RuntimeError(msg)


def on_backend_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Return True when the calling thread is the one running ``loop``.

    A sync adapter hands its work to ``loop`` and blocks on the result,
    which never arrives when the caller is that loop. A caller on another
    loop, or on a thread with no loop at all, is served normally.
    """
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def raise_event_loop_deadlock(what: str, instead: str) -> NoReturn:
    """Raise for a sync entry point used on the loop that has to serve it.

    Call this only on the failure branch of ``if on_backend_loop(loop)``.
    ``what`` names the caller, for example ``"Lock 'orders' `from_thread`"``.
    ``instead`` is the sentence that tells the caller the way in.

    Raises:
        EventLoopDeadlockError: Always. Not an ``Exception``, so
            ``except Exception``, a retry, and a fallback all pass it
            through.
    """
    msg = (
        f"{what} was used from the event loop that has to do the work, so "
        "the call would wait forever on the loop it is blocking. "
        f"{instead}"
    )
    raise EventLoopDeadlockError(msg)


async def sleep_or_stop(seconds: float, stop: asyncio.Event | None) -> bool:
    """Sleep up to ``seconds``, waking early when ``stop`` is set.

    Returns ``True`` when a stop was requested (either already set or
    raised during the wait), so a background loop can break and unwind
    cleanly. Returns ``False`` when the full interval elapsed and the
    loop should run again. With ``stop`` of ``None`` this is a plain
    ``asyncio.sleep`` that always returns ``False``.
    """
    if stop is None:
        await asyncio.sleep(seconds)
        return False
    if stop.is_set():
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def run_to_completion[T](work: Coroutine[Any, Any, T]) -> T:
    """Await `work` to its end, even when the caller is cancelled meanwhile.

    `work` runs as a task of its own, and the caller keeps waiting for it
    through every cancellation delivered in the meantime. Once `work` has
    ended, a cancellation the caller received is raised, whatever `work`
    returned or raised. Otherwise its result is returned, or its error
    raised.

    `work` has to bound its own wait, since nothing here gives up on it.
    """
    task = asyncio.ensure_future(work)
    _finishing.add(task)
    task.add_done_callback(_finishing.discard)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            # Waiting never cancels `task` and never raises its error.
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return task.result()


def is_async_callable(
    obj: Any,  # noqa: ANN401
) -> TypeGuard[Callable[..., Awaitable[Any]]]:
    """Return True if ``obj`` is an async callable.

    Unwraps nested ``functools.partial`` wrappers, then checks both
    the object itself and its ``__call__``, so partials of async
    functions and callable instances with ``async def __call__`` are
    both recognised.

    Narrows like ``inspect.iscoroutinefunction`` does, so a decorator
    picking its wrapper can await what it has just recognised.
    """
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(obj.__call__)
    )
