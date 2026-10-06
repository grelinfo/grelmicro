"""Duration type for a stored or enforced duration.

A field typed `Duration` takes whole seconds as an `int` or a
`timedelta`, and always holds a `timedelta` once validated.
"""

import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, ValidationInfo

MAX_DURATION = timedelta(days=36_500)
"""Longest duration, a hundred years."""

_MAX_SECONDS = MAX_DURATION // timedelta(seconds=1)

_MICROSECOND_DIGITS = 6

_WHOLE_SECONDS = re.compile(r"-?[0-9]+")

_ISO_8601 = re.compile(
    r"P(?:([0-9]+)W)?(?:([0-9]+)D)?"
    r"(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)(?:[.,]([0-9]+))?S)?)?"
)
"""An ISO 8601 duration in weeks, days, hours, minutes and seconds.

Only the seconds take a fraction. Years and months are left out, since
their length depends on the calendar.
"""


def _name(info: ValidationInfo) -> str:
    """Return the field name, or `duration` outside a field."""
    return info.field_name or "duration"


def _from_seconds(seconds: int, info: ValidationInfo) -> timedelta:
    """Return whole `seconds` as a timedelta, refused out of range."""
    if seconds <= 0:
        msg = f"{_name(info)} must be greater than zero"
        raise ValueError(msg)
    if seconds > _MAX_SECONDS:
        msg = f"{_name(info)} must be at most 100 years"
        raise ValueError(msg)
    return timedelta(seconds=seconds)


def _from_text(text: str, info: ValidationInfo) -> timedelta:
    """Read whole seconds (`"60"`) or an ISO 8601 duration (`"PT0.5S"`)."""
    if _WHOLE_SECONDS.fullmatch(text):
        return _from_seconds(int(text), info)
    match = _ISO_8601.fullmatch(text)
    if match is None or text == "P" or text.endswith("T"):
        msg = f"{_name(info)} must be whole seconds or an ISO 8601 duration"
        raise ValueError(msg)
    weeks, days, hours, minutes, seconds, fraction = match.groups()
    fraction = (fraction or "").rstrip("0")
    if len(fraction) > _MICROSECOND_DIGITS:
        msg = f"{_name(info)} must be whole microseconds"
        raise ValueError(msg)
    try:
        return timedelta(
            weeks=int(weeks or 0),
            days=int(days or 0),
            hours=int(hours or 0),
            minutes=int(minutes or 0),
            seconds=int(seconds or 0),
            microseconds=int(fraction.ljust(_MICROSECOND_DIGITS, "0")),
        )
    except OverflowError:
        msg = f"{_name(info)} must be at most 100 years"
        raise ValueError(msg) from None


def _parse(value: Any, info: ValidationInfo) -> Any:  # noqa: ANN401
    """Read whole seconds or text, and refuse any other number.

    A `timedelta` goes on unchanged. A float, a bool, or any other number
    is refused.
    """
    if isinstance(value, str):
        return _from_text(value, info)
    if isinstance(value, int) and not isinstance(value, bool):
        return _from_seconds(value, info)
    if isinstance(value, timedelta):
        return value
    msg = f"{_name(info)} must be whole seconds or a timedelta"
    raise ValueError(msg)


def _check_range(value: timedelta, info: ValidationInfo) -> timedelta:
    """Refuse a duration of zero or less, or over a hundred years."""
    if value <= timedelta(0):
        msg = f"{_name(info)} must be greater than zero"
        raise ValueError(msg)
    if value > MAX_DURATION:
        msg = f"{_name(info)} must be at most 100 years"
        raise ValueError(msg)
    return value


Duration = Annotated[
    timedelta,
    BeforeValidator(_parse),
    AfterValidator(_check_range),
]
"""A positive duration of at most a hundred years.

Takes whole seconds as an `int`, or a `timedelta`. A float or a bool is
refused. From text, such as an environment variable, it reads whole
seconds (`"60"`) or an ISO 8601 duration in weeks, days, hours, minutes
and seconds (`"PT0.5S"`), exact to the microsecond. A decimal number of
seconds, such as `"1.5"`, is refused, and so are years and months.
"""
