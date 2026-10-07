"""Duration type for a stored or enforced duration.

A field typed `Duration` takes whole seconds as an `int` or a
`timedelta`, and always holds a `timedelta` once validated.
"""

import re
from datetime import timedelta
from math import floor, isfinite
from time import time_ns
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BeforeValidator,
    PlainSerializer,
    ValidationInfo,
)

MICROSECOND = timedelta(microseconds=1)
"""One microsecond, the finest step a `timedelta` takes."""

MILLISECOND = timedelta(milliseconds=1)
"""One millisecond."""

SECOND = timedelta(seconds=1)
"""One second."""

NANOSECONDS_PER_SECOND = 1_000_000_000
"""Nanoseconds in one second, for a clock read in whole nanoseconds."""

MAX_DURATION = timedelta(days=36_500)
"""Longest duration, a hundred years."""

_ZERO = timedelta(0)
"""A duration of nothing, which no duration may be."""

_TOO_LONG = MAX_DURATION + MICROSECOND

_MAX_SECONDS = MAX_DURATION // SECOND

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


def microseconds(duration: timedelta) -> int:
    """Return `duration` in whole microseconds, exactly."""
    return duration // MICROSECOND


def nanoseconds(duration: timedelta) -> int:
    """Return `duration` in whole nanoseconds, exactly."""
    return duration // MICROSECOND * 1_000


_MICROSECONDS_PER_SECOND = SECOND // MICROSECOND

_NANOSECONDS_PER_MICROSECOND = 1_000


def seconds_to_microseconds(seconds: float) -> int:
    """Return a number of seconds in whole microseconds, to the nearest.

    A count of microseconds written as seconds with
    `microseconds_to_seconds` reads back exactly.
    """
    return round(seconds * _MICROSECONDS_PER_SECOND)


def microseconds_to_seconds(count: int) -> float:
    """Return a count of whole microseconds as seconds."""
    return count / _MICROSECONDS_PER_SECOND


def clock_microseconds() -> int:
    """Return the wall clock in whole microseconds since the epoch, rounded down."""
    return time_ns() // _NANOSECONDS_PER_MICROSECOND


def clock_microseconds_up() -> int:
    """Return the wall clock in whole microseconds since the epoch, rounded up.

    The time it returns is never before the instant it reads.
    """
    return -(-time_ns() // _NANOSECONDS_PER_MICROSECOND)


def nanoseconds_from_seconds(seconds: float) -> int:
    """Return a number of seconds from outside, in whole nanoseconds.

    For a float a server or a wait setting gives. Rounded down, and capped
    at a hundred years.
    """
    return floor(min(seconds, _MAX_SECONDS) * NANOSECONDS_PER_SECOND)


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


def _from_text(text: str, name: str) -> timedelta:
    """Read whole seconds (`"60"`) or an ISO 8601 duration (`"PT0.5S"`)."""
    if _WHOLE_SECONDS.fullmatch(text):
        return _from_seconds(_number(text))
    match = _ISO_8601.fullmatch(text)
    if match is None:
        msg = f"{name} must be whole seconds or an ISO 8601 duration"
        raise ValueError(msg)
    weeks, days, hours, minutes, seconds, fraction = match.groups()
    fraction = (fraction or "").rstrip("0")
    if len(fraction) > _MICROSECOND_DIGITS:
        msg = f"{name} must be whole microseconds"
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


def _from_value(value: object, name: str) -> timedelta:
    """Read whole seconds or a `timedelta`, and refuse anything else.

    A float, a bool, text, or any other number is refused.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return _from_seconds(value)
    if isinstance(value, timedelta):
        return value
    msg = f"{name} must be whole seconds or a timedelta"
    raise ValueError(msg)


def in_range(value: timedelta, name: str) -> timedelta:
    """Return `value`, refusing a duration of zero or less, or over 100 years.

    Raises:
        ValueError: If `value` is not greater than zero, or is over 100
            years. The message names `name`.
    """
    if value <= _ZERO:
        msg = f"{name} must be greater than zero"
        raise ValueError(msg)
    return _at_most_a_century(value, name)


def _at_most_a_century(value: timedelta, name: str) -> timedelta:
    """Return `value`, refusing a duration over 100 years, named `name`."""
    if value > MAX_DURATION:
        msg = f"{name} must be at most 100 years"
        raise ValueError(msg)
    return value


def check_finite(value: float, name: str) -> float:
    """Return a wait of float seconds, refusing one that is not finite.

    Raises:
        ValueError: If `value` is NaN or infinite. The message names `name`
            and never the value.
    """
    if not isfinite(value):
        msg = f"{name} must be a finite number"
        raise ValueError(msg)
    return value


def _read(value: object, name: str) -> timedelta:
    """Read whole seconds, a `timedelta`, or text, and refuse a float."""
    if isinstance(value, str):
        return _from_text(value, name)
    return _from_value(value, name)


def _parse(value: Any, info: ValidationInfo) -> Any:  # noqa: ANN401
    """Read whole seconds, a `timedelta`, or text, and refuse a float."""
    return _read(value, _name(info))


def _check_range(value: timedelta, info: ValidationInfo) -> timedelta:
    """Refuse a duration of zero or less, or over a hundred years."""
    return in_range(value, _name(info))


def _check_cache_range(value: timedelta, info: ValidationInfo) -> timedelta:
    """Refuse a cache TTL below zero, or over a hundred years."""
    name = _name(info)
    if value < _ZERO:
        msg = f"{name} must not be negative"
        raise ValueError(msg)
    return _at_most_a_century(value, name)


def _to_text(value: timedelta) -> str:
    """Write `value` as ISO 8601 in days and smaller units.

    Days take the place of years, so the text reads back exactly. A
    duration of nothing is written `PT0S`.
    """
    if not value:
        return "PT0S"
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

CacheTTL = Annotated[
    timedelta,
    BeforeValidator(_parse),
    AfterValidator(_check_cache_range),
    PlainSerializer(_to_text, when_used="json"),
]
"""How long a component's built-in cache keeps an entry, zero for off.

Takes what a `Duration` takes, and zero too (`0`, `timedelta(0)`, `"0"`
or `"PT0S"`), which turns that cache off. A negative value, or one over a
hundred years, is refused.
"""


def check_duration(value: int | timedelta, name: str) -> timedelta:
    """Return a typed argument as a duration, refused under its own name.

    Takes whole seconds as an `int`, or a `timedelta`, in the range a
    `Duration` field takes. Text is refused. A refusal says what a
    `Duration` field says, naming `name`.

    Raises:
        ValueError: If `value` is not whole seconds or a `timedelta`, is
            not greater than zero, or is over 100 years.
    """
    if isinstance(value, timedelta) and _ZERO < value <= MAX_DURATION:
        return value
    return in_range(_from_value(value, name), name)


def read_duration(value: object, name: str) -> timedelta:
    """Return a configured value as a duration, refused under its own name.

    Takes what a `Duration` field takes, text included, for a value read
    from a config or the environment that sits where no field names it,
    such as one value of a mapping. A refusal names `name`.

    Raises:
        ValueError: If `value` is not a valid duration.
    """
    return in_range(_read(value, name), name)
