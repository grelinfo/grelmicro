"""Shared async utilities."""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any


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


def is_async_callable(obj: Any) -> bool:  # noqa: ANN401
    """Return True if ``obj`` is an async callable.

    Unwraps nested ``functools.partial`` wrappers, then checks both
    the object itself and its ``__call__``. Mirrors Starlette's
    detection (``starlette._utils.is_async_callable``) so partials
    of async functions and callable instances with
    ``async def __call__`` are both recognised.
    """
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(obj.__call__)
    )
