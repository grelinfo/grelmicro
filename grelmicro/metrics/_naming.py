"""How a callable is named in telemetry.

A metric name and a metric attribute are both a time series key, so the
name a callable is given has to be the same on every run of the process.
`repr()` is not: it carries the memory address of the object, which is a
new series on every restart.
"""

from __future__ import annotations

import functools
from typing import Any


def unwrap_callable(func: Any) -> Any:  # noqa: ANN401
    """Return the function behind `func`, past any `functools.partial`.

    A partial carries no name of its own and reports `functools` as its
    module, so the function it wraps is what a reader wants named.
    """
    target = func
    while isinstance(target, functools.partial):
        target = target.func
    return target


def callable_name(func: Any) -> str:  # noqa: ANN401
    """Return a stable, bounded name for `func`.

    Falls back to the type name rather than `repr()` for a callable
    object, because a `repr` carries a memory address and every restart
    would open a new time series.
    """
    target = unwrap_callable(func)
    name = getattr(target, "__qualname__", None) or getattr(
        target, "__name__", None
    )
    if name is None:
        name = type(target).__name__
    return name.replace(".<locals>", "")
