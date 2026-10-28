"""Task Utilities.

`validate_and_generate_reference` is adapted from an upstream project.
See THIRD_PARTY_NOTICES.md for the source, license, and changes.
"""

from collections.abc import Callable
from datetime import tzinfo
from functools import partial
from inspect import ismethod
from typing import Any

from grelmicro._timezone import (
    normalize_timezone_name,
)
from grelmicro._timezone import (
    resolve_timezone as _resolve_timezone,
)
from grelmicro.task.errors import FunctionTypeError, TimezoneError


def resolve_timezone(timezone: str) -> tzinfo:
    """Resolve an IANA timezone name into a ``tzinfo``.

    Raises:
        TimezoneError: If no timezone of that name can be loaded.
    """
    try:
        return _resolve_timezone(timezone)
    except ValueError as error:
        raise TimezoneError(str(error)) from None


def normalize_timezone(timezone: str) -> str:
    """Return an IANA timezone name in the casing the database uses.

    Raises:
        TimezoneError: If no timezone of that name can be loaded.
    """
    try:
        return normalize_timezone_name(timezone)
    except ValueError as error:
        raise TimezoneError(str(error)) from None


def validate_and_generate_reference(function: Callable[..., Any]) -> str:
    """Build a stable ``module:qualname`` reference for a task function.

    The reference must survive process restarts and round-trip through
    serialization, so only top-level ``def`` and ``async def`` callables
    are accepted. Anything whose identity depends on a closure, a bound
    instance, or runtime construction is rejected.

    The returned reference surfaces in logs, distributed coordination
    keys, and metric labels. For tasks that handle sensitive workflows,
    pass an explicit ``name=`` to the task decorator or registration
    call instead of relying on the auto-generated module path.

    Raises:
        FunctionTypeError: If ``function`` cannot be referenced by name.

    """
    if isinstance(function, partial):
        ref = "partial()"
        raise FunctionTypeError(ref)

    if ismethod(function):
        ref = "method"
        raise FunctionTypeError(ref)

    module = getattr(function, "__module__", None)
    qualname = getattr(function, "__qualname__", None)
    if not module or not qualname:
        ref = "callable without __module__ or __qualname__ attribute"
        raise FunctionTypeError(ref)

    if "<lambda>" in qualname:
        ref = "lambda"
        raise FunctionTypeError(ref)

    if "<locals>" in qualname:
        ref = "nested function"
        raise FunctionTypeError(ref)

    return f"{module}:{qualname}"
