"""Tests for the shared duration type."""

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from grelmicro._duration import Duration


class _Model(BaseModel):
    ttl: Duration


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (60, timedelta(seconds=60)),
        (timedelta(milliseconds=500), timedelta(milliseconds=500)),
        ("60", timedelta(seconds=60)),
        ("PT0.5S", timedelta(milliseconds=500)),
        ("PT1,25S", timedelta(milliseconds=1250)),
        ("PT0.000001S", timedelta(microseconds=1)),
        ("PT0.5000000S", timedelta(milliseconds=500)),
        (
            "P1W2DT3H4M5S",
            timedelta(weeks=1, days=2, hours=3, minutes=4, seconds=5),
        ),
        ("P36500D", timedelta(days=36_500)),
        (timedelta(days=36_500), timedelta(days=36_500)),
    ],
)
def test_duration_takes_whole_seconds_or_a_timedelta(
    raw: object, expected: timedelta
) -> None:
    """Whole seconds, a timedelta, or text of either, read as a timedelta."""
    assert _Model.model_validate({"ttl": raw}).ttl == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (1.5, "ttl must be whole seconds or a timedelta"),
        (60.0, "ttl must be whole seconds or a timedelta"),
        (True, "ttl must be whole seconds or a timedelta"),
        (Decimal("1.5"), "ttl must be whole seconds or a timedelta"),
        (Fraction(3, 2), "ttl must be whole seconds or a timedelta"),
        ("1.5", "ttl must be whole seconds or an ISO 8601 duration"),
        ("1_000", "ttl must be whole seconds or an ISO 8601 duration"),
        (" 60 ", "ttl must be whole seconds or an ISO 8601 duration"),
        ("+60", "ttl must be whole seconds or an ISO 8601 duration"),
        ("\u0666\u0660", "ttl must be whole seconds or an ISO 8601 duration"),
        ("1:00:00", "ttl must be whole seconds or an ISO 8601 duration"),
        ("P1Y", "ttl must be whole seconds or an ISO 8601 duration"),
        ("P1M", "ttl must be whole seconds or an ISO 8601 duration"),
        ("PT0.5M", "ttl must be whole seconds or an ISO 8601 duration"),
        ("-PT1S", "ttl must be whole seconds or an ISO 8601 duration"),
        ("P", "ttl must be whole seconds or an ISO 8601 duration"),
        ("PT", "ttl must be whole seconds or an ISO 8601 duration"),
        ("PT0.0000005S", "ttl must be whole microseconds"),
        ("PT0S", "ttl must be greater than zero"),
        ("0", "ttl must be greater than zero"),
        ("-5", "ttl must be greater than zero"),
        ("P36501D", "ttl must be at most 100 years"),
        (10**20, "ttl must be at most 100 years"),
        ("P99999999999999D", "ttl must be at most 100 years"),
        ("PT99999999999999999999H", "ttl must be at most 100 years"),
        (-(10**20), "ttl must be greater than zero"),
        (0, "ttl must be greater than zero"),
        (-1, "ttl must be greater than zero"),
        (timedelta(0), "ttl must be greater than zero"),
        (timedelta(days=36_501), "ttl must be at most 100 years"),
    ],
)
def test_duration_refuses_a_float_or_a_value_out_of_range(
    raw: object, message: str
) -> None:
    """A float, a bool, or a duration out of range is refused."""
    with pytest.raises(ValidationError, match=message):
        _Model.model_validate({"ttl": raw})


def test_duration_refusal_leaves_out_the_value() -> None:
    """The refusal message does not repeat the rejected value."""
    with pytest.raises(ValidationError) as excinfo:
        _Model.model_validate({"ttl": "1.25"})

    assert "1.25" not in excinfo.value.errors()[0]["msg"]


def test_duration_outside_a_field_names_a_duration() -> None:
    """A duration validated outside a model field is called a duration."""
    with pytest.raises(ValidationError, match="duration must be whole"):
        TypeAdapter(Duration).validate_python(1.5)
