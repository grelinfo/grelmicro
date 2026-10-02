"""Tests for the shared duration type."""

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import BaseModel, ValidationError

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
        ("1.5", "ttl must be whole seconds or a timedelta"),
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
