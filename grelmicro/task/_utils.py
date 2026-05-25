"""Task Utilities.

`validate_and_generate_reference` is adapted from an upstream project.
See THIRD_PARTY_NOTICES.md for the source, license, and changes.
"""

from collections.abc import Callable
from functools import partial
from inspect import ismethod
from typing import Any

from grelmicro.task.errors import FunctionTypeError


def validate_and_generate_reference(function: Callable[..., Any]) -> str:
    """Build a stable ``module:qualname`` reference for a task function.

    The reference must survive process restarts and round-trip through
    serialization, so only top-level ``def`` and ``async def`` callables
    are accepted. Anything whose identity depends on a closure, a bound
    instance, or runtime construction is rejected.

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
