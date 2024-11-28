"""Task Utilities."""

from collections.abc import Callable
from functools import partial
from inspect import ismethod
from typing import Any

from grelmicro.task.errors import FunctionTypeError


def validate_and_generate_reference(function: Callable[..., Any]) -> str:
    """Generate a task name from the given function.

    This implementation is inspirated by the APScheduler project under MIT License.
    Original source: https://github.com/agronholm/apscheduler/blob/master/src/apscheduler/_marshalling.py

    Raises:
        FunctionNotSupportedError: If function is not supported.

    """
    if isinstance(function, partial):
        ref = "partial()"
        raise FunctionTypeError(ref)

    if ismethod(function):
        ref = "method"
        raise FunctionTypeError(ref)

    if not hasattr(function, "__module__") or not hasattr(
        function, "__qualname__"
    ):
        ref = "callable without __module__ or __qualname__ attribute"
        raise FunctionTypeError(ref)

    if "<lambda>" in function.__qualname__:
        ref = "lambda"
        raise FunctionTypeError(ref)

    if "<locals>" in function.__qualname__:
        ref = "nested function"
        raise FunctionTypeError(ref)

    return f"{function.__module__}:{function.__qualname__}"
