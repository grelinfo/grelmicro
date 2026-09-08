"""How a callable is named in telemetry.

A metric name and a metric attribute are both a time series key, so the
name a callable is given has to be the same on every run of the process.
`repr()` is not: it carries the memory address of the object, which is a
new series on every restart.
"""

from __future__ import annotations

import functools
import re
from typing import Any

_VALID_METRIC_NAME = re.compile(r"[a-zA-Z][-_./a-zA-Z0-9]{0,254}")
"""What the OpenTelemetry specification accepts as an instrument name."""

_INVALID_CHARACTER = re.compile(r"[^-_./a-zA-Z0-9]")
"""Everything the specification refuses inside an instrument name."""

_NAME_LIMIT = 255
"""Longest instrument name the specification accepts."""


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

    Falls back to the type's qualified name rather than `repr()` for a
    callable object, because a `repr` carries a memory address and every
    restart would open a new time series. The type's *qualified* name,
    so two classes of the same name in one module stay apart.
    """
    target = unwrap_callable(func)
    name = getattr(target, "__qualname__", None) or getattr(
        target, "__name__", None
    )
    if name is None:
        name = type(target).__qualname__
    return name.replace(".<locals>", "")


def metric_name(name: str) -> str:
    """Return `name` as something OpenTelemetry accepts as an instrument name.

    An instrument name has to start with a letter and hold only letters,
    digits, and `-_./`. A name derived from a function breaks both rules
    on shapes that are ordinary Python: a function in the entry-point
    script is `__main__.charge`, and a lambda is `<lambda>`. The SDK
    raises on either, from inside the call the decorator was meant to
    watch, and only once metrics are turned on. So an app that ran in
    development would fail in production.

    A name that is already valid is returned unchanged, so no metric
    that works today is renamed.
    """
    if _VALID_METRIC_NAME.fullmatch(name):
        return name
    segments = (
        _INVALID_CHARACTER.sub("_", segment).strip("_")
        for segment in name.split(".")
    )
    cleaned = ".".join(segment for segment in segments if segment)
    while cleaned and not ("a" <= cleaned[0].lower() <= "z"):
        cleaned = cleaned[1:]
    return cleaned[:_NAME_LIMIT] or "unnamed"
