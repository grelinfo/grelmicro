"""Duration type for a stored or enforced duration.

A field typed `Duration` takes whole seconds as an `int` or a
`timedelta`, and always holds a `timedelta` once validated.
"""

import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BeforeValidator,
    PlainSerializer,
    ValidationInfo,
)

MAX_DURATION = timedelta(days=36_500)
"""Longest duration, a hundred years."""

_TOO_LONG = MAX_DURATION + timedelta(microseconds=1)

_MAX_SECONDS = MAX_DURATION // timedelta(seconds=1)

_MICROSECOND_DIGITS = 6

_WHOLE_SECONDS = re.compile(r"-?[0-9]+")

_ISO_8601 = re.compile(
    r"P(?=[0-9]|T[0-9])(?:([0-9]+)W)?(?:([0-9]+)D)?"
    r"(?:T(?=[0-9])(?:([0-9]+)H)?(?:([0-9]+)M)?"
    r"(?:([0-9]+)(?:[.,]([0-9]+))?S)?)?"
)
"""An ISO 8601 duration in weeks, days, hours, minutes and seconds.

Only the seconds take a fraction. Years and months are left out, since
their length depends on the calendar.
"""


def round_up(duration: timedelta, unit: timedelta) -> int:
    """Return `duration` in whole `unit`s, rounded up.

    A duration that is an exact multiple of `unit` keeps its count. Any
    remainder adds one more unit.
    """
    return -(-duration // unit)


def _name(info: ValidationInfo) -> str:
    """Return the field name, or `duration` outside a field."""
    return info.field_name or "duration"


def _number(digits: str | None) -> int:
    """Return `digits` as an int, capped just past a hundred years."""
    if digits is None:
        return 0
    if len(digits.lstrip("-0")) > len(str(_MAX_SECONDS)):
        return -_MAX_SECONDS - 1 if digits.startswith("-") else _MAX_SECONDS + 1
    return int(digits)


def _from_seconds(seconds: int) -> timedelta:
    """Return whole `seconds` as a timedelta.

    Seconds out of range come back just outside it, so the range check
    refuses them with its own message.
    """
    return timedelta(seconds=max(-1, min(seconds, _MAX_SECONDS + 1)))


def _from_text(text: str, info: ValidationInfo) -> timedelta:
    """Read whole seconds (`"60"`) or an ISO 8601 duration (`"PT0.5S"`)."""
    if _WHOLE_SECONDS.fullmatch(text):
        return _from_seconds(_number(text))
    match = _ISO_8601.fullmatch(text)
    if match is None:
        msg = f"{_name(info)} must be whole seconds or an ISO 8601 duration"
        raise ValueError(msg)
    weeks, days, hours, minutes, seconds, fraction = match.groups()
    fraction = (fraction or "").rstrip("0")
    if len(fraction) > _MICROSECOND_DIGITS:
        msg = f"{_name(info)} must be whole microseconds"
        raise ValueError(msg)
    try:
        return timedelta(
            weeks=_number(weeks),
            days=_number(days),
            hours=_number(hours),
            minutes=_number(minutes),
            seconds=_number(seconds),
            microseconds=int(fraction.ljust(_MICROSECOND_DIGITS, "0")),
        )
    except OverflowError:
        return _TOO_LONG


def _parse(value: Any, info: ValidationInfo) -> Any:  # noqa: ANN401
    """Read whole seconds or text, and refuse any other number.

    A `timedelta` goes on unchanged. A float, a bool, or any other number
    is refused.
    """
    if isinstance(value, str):
        return _from_text(value, info)
    if isinstance(value, int) and not isinstance(value, bool):
        return _from_seconds(value)
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


def _to_text(value: timedelta) -> str:
    """Write `value` as ISO 8601 in days and smaller units.

    Days take the place of years, so the text reads back exactly.
    """
    minutes, seconds = divmod(value.seconds, 60)
    hours, minutes = divmod(minutes, 60)
    text = f"P{value.days}D" if value.days else "P"
    time = "".join(
        f"{amount}{unit}"
        for amount, unit in ((hours, "H"), (minutes, "M"))
        if amount
    )
    if value.microseconds:
        time += f"{seconds}.{value.microseconds:06d}".rstrip("0") + "S"
    elif seconds:
        time += f"{seconds}S"
    return f"{text}T{time}" if time else text


Duration = Annotated[
    timedelta,
    BeforeValidator(_parse),
    AfterValidator(_check_range),
    PlainSerializer(_to_text, when_used="json"),
]
"""A positive duration of at most a hundred years.

Takes whole seconds as an `int`, or a `timedelta`. A float or a bool is
refused. From text, such as an environment variable, it reads whole
seconds (`"60"`) or an ISO 8601 duration in weeks, days, hours, minutes
and seconds (`"PT0.5S"`), exact to the microsecond. A decimal number of
seconds, such as `"1.5"`, is refused, and so are years and months.
In JSON it is written the same way, in days and smaller units.
"""
