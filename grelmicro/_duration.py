"""Duration type for a stored or enforced duration.

A field typed `Duration` takes whole seconds as an `int` or a
`timedelta`, and always holds a `timedelta` once validated.
"""

from datetime import timedelta
from numbers import Number
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, ValidationInfo

MAX_DURATION = timedelta(days=36_500)
"""Longest duration, a hundred years."""


def _refuse_float(value: Any, info: ValidationInfo) -> Any:  # noqa: ANN401
    """Refuse a number other than an `int`, a bool, or text of a decimal.

    Text of whole seconds, such as `"60"`, reads as an `int`. Any other
    value goes on to Pydantic, which reads an ISO 8601 duration such as
    `"PT0.5S"`.
    """
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            float(value)
        except ValueError:
            return value
    elif not isinstance(value, bool) and (
        isinstance(value, int) or not isinstance(value, Number)
    ):
        return value
    msg = f"{info.field_name} must be whole seconds or a timedelta"
    raise ValueError(msg)


def _check_range(value: timedelta, info: ValidationInfo) -> timedelta:
    """Refuse a duration of zero or less, or over a hundred years."""
    if value <= timedelta(0):
        msg = f"{info.field_name} must be greater than zero"
        raise ValueError(msg)
    if value > MAX_DURATION:
        msg = f"{info.field_name} must be at most 100 years"
        raise ValueError(msg)
    return value


Duration = Annotated[
    timedelta,
    BeforeValidator(_refuse_float),
    AfterValidator(_check_range),
]
"""A positive duration of at most a hundred years.

Takes whole seconds as an `int`, or a `timedelta`. A float or a bool is
refused. From text, such as an environment variable, it reads whole
seconds (`"60"`) or an ISO 8601 duration (`"PT0.5S"`). A decimal number
of seconds, such as `"1.5"`, is refused.
"""
