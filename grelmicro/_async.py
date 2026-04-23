"""Shared async utilities."""

from __future__ import annotations

import functools
import inspect
from typing import Any


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
