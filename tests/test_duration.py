"""Tests for the shared duration type."""

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from grelmicro import _duration as duration_module
from grelmicro._duration import (
    MAX_DURATION,
    MICROSECOND,
    MILLISECOND,
    Duration,
    check_duration,
    clock_microseconds,
    clock_microseconds_up,
    microseconds,
    microseconds_to_seconds,
    nanoseconds,
    read_duration,
    round_up,
    seconds_to_microseconds,
)


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
        pytest.param(
            "9" * 5000, "ttl must be at most 100 years", id="5000-digits"
        ),
        pytest.param(
            f"P{'9' * 5000}D", "ttl must be at most 100 years", id="5000-days"
        ),
        ("P1DT", "ttl must be whole seconds or an ISO 8601 duration"),
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


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(seconds=60), "PT1M"),
        (timedelta(seconds=90), "PT1M30S"),
        (timedelta(milliseconds=1500), "PT1.5S"),
        (timedelta(days=7), "P7D"),
        (timedelta(days=1, hours=2, microseconds=3), "P1DT2H0.000003S"),
        (timedelta(days=400), "P400D"),
        (timedelta(days=36_500), "P36500D"),
    ],
)
def test_duration_dumps_to_json_it_reads_back(
    value: timedelta, text: str
) -> None:
    """A duration dumps to ISO 8601 in days and smaller units, read back."""
    dumped = _Model(ttl=value).model_dump(mode="json")["ttl"]

    assert dumped == text
    assert _Model.model_validate({"ttl": dumped}).ttl == value


@given(
    st.timedeltas(min_value=timedelta(microseconds=1), max_value=MAX_DURATION)
)
def test_every_duration_reads_back_from_its_json(value: timedelta) -> None:
    """Any duration in range reads back from its JSON text exactly."""
    dumped = _Model(ttl=value).model_dump(mode="json")["ttl"]

    assert _Model.model_validate({"ttl": dumped}).ttl == value


@pytest.mark.parametrize(
    ("duration", "unit", "expected"),
    [
        (timedelta(seconds=2), timedelta(seconds=1), 2),
        (timedelta(seconds=2, microseconds=1), timedelta(seconds=1), 3),
        (timedelta(milliseconds=1001), timedelta(milliseconds=1), 1001),
        (timedelta(microseconds=1_000_001), timedelta(milliseconds=1), 1001),
        (timedelta(microseconds=1), timedelta(milliseconds=1), 1),
        (
            timedelta(microseconds=1_000_001),
            timedelta(microseconds=1),
            1_000_001,
        ),
        (MAX_DURATION, timedelta(seconds=1), 3_153_600_000),
    ],
)
def test_duration_round_up_counts_whole_units_never_fewer(
    duration: timedelta, unit: timedelta, expected: int
) -> None:
    """An exact multiple keeps its count, and one microsecond over adds a unit."""
    assert round_up(duration, unit) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (60, timedelta(seconds=60)),
        (timedelta(milliseconds=500), timedelta(milliseconds=500)),
        (MAX_DURATION, MAX_DURATION),
    ],
)
def test_duration_check_returns_a_timedelta(
    raw: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds or a timedelta, passed as an argument, read as a timedelta."""
    # Act
    checked = check_duration(raw, "stale_ttl")

    # Assert
    assert checked == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (1.5, r"^stale_ttl must be whole seconds or a timedelta$"),
        (True, r"^stale_ttl must be whole seconds or a timedelta$"),
        ("60", r"^stale_ttl must be whole seconds or a timedelta$"),
        ("PT0.5S", r"^stale_ttl must be whole seconds or a timedelta$"),
        (0, r"^stale_ttl must be greater than zero$"),
        (timedelta(0), r"^stale_ttl must be greater than zero$"),
        (10**20, r"^stale_ttl must be at most 100 years$"),
        (timedelta(days=36_501), r"^stale_ttl must be at most 100 years$"),
    ],
)
def test_duration_check_refusal_names_the_argument(
    raw: object, message: str
) -> None:
    """A float, a bool, text, or a value out of range is refused by name."""
    # Act / Assert
    with pytest.raises(ValueError, match=message) as excinfo:
        check_duration(raw, "stale_ttl")  # ty: ignore[invalid-argument-type]

    # Assert
    assert not isinstance(excinfo.value, ValidationError)


def test_duration_check_runs_no_pydantic_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed argument is checked without a Pydantic validation."""

    # Arrange
    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "Pydantic validation ran"
        raise AssertionError(msg)

    monkeypatch.setattr(TypeAdapter, "validate_python", refuse)

    # Act
    checked = check_duration(60, "ttl")

    # Assert
    assert checked == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (60, timedelta(seconds=60)),
        ("60", timedelta(seconds=60)),
        ("PT0.5S", timedelta(milliseconds=500)),
        (timedelta(milliseconds=500), timedelta(milliseconds=500)),
    ],
)
def test_duration_read_takes_text_whole_seconds_or_a_timedelta(
    raw: object, expected: timedelta
) -> None:
    """A configured value reads text as well as whole seconds or a timedelta."""
    # Act
    read = read_duration(raw, "pattern 1 in include")

    # Assert
    assert read == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (1.5, r"^pattern 1 in include must be whole seconds or a timedelta$"),
        (
            "1.5",
            (
                r"^pattern 1 in include must be whole seconds or an ISO 8601 "
                r"duration$"
            ),
        ),
        ("0", r"^pattern 1 in include must be greater than zero$"),
        (0, r"^pattern 1 in include must be greater than zero$"),
        ("P36501D", r"^pattern 1 in include must be at most 100 years$"),
    ],
)
def test_duration_read_refusal_names_where_it_sits(
    raw: object, message: str
) -> None:
    """A refused configured value is named by where it sits."""
    # Act / Assert
    with pytest.raises(ValueError, match=message):
        read_duration(raw, "pattern 1 in include")


@pytest.mark.parametrize(
    ("duration", "micro", "nano"),
    [
        (MICROSECOND, 1, 1_000),
        (MILLISECOND, 1_000, 1_000_000),
        (MAX_DURATION, 3_153_600_000_000_000, 3_153_600_000_000_000_000),
        (timedelta(seconds=-7200), -7_200_000_000, -7_200_000_000_000),
    ],
)
def test_duration_whole_units_are_exact(
    duration: timedelta, micro: int, nano: int
) -> None:
    """Whole microseconds and nanoseconds are exact, with no float."""
    # Act / Assert
    assert (microseconds(duration), nanoseconds(duration)) == (micro, nano)


EPOCH_MICROSECONDS = 1_800_000_000_000_000
"""An epoch time in whole microseconds, past 2^50."""

READING_NS = 1_700_000_000_000_000_500
"""A wall clock reading in nanoseconds that is not a whole microsecond."""


@given(st.integers(min_value=0, max_value=microseconds(MAX_DURATION)))
def test_duration_microseconds_written_as_seconds_read_back_exactly(
    count: int,
) -> None:
    """Whole microseconds written as seconds read back as the same count."""
    # Act
    read_back = seconds_to_microseconds(microseconds_to_seconds(count))

    # Assert
    assert read_back == count


def test_duration_epoch_microseconds_written_as_seconds_read_back_exactly() -> (
    None
):
    """An epoch time in microseconds survives a trip through seconds."""
    # Act
    read_back = seconds_to_microseconds(
        microseconds_to_seconds(EPOCH_MICROSECONDS + 1)
    )

    # Assert
    assert read_back == EPOCH_MICROSECONDS + 1


def test_duration_clock_microseconds_rounds_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall clock in microseconds drops a part microsecond."""
    # Arrange
    monkeypatch.setattr(duration_module, "time_ns", lambda: READING_NS)

    # Act
    now = clock_microseconds()

    # Assert
    assert now == READING_NS // 1_000


def test_duration_clock_microseconds_up_rounds_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall clock in microseconds, rounded up, is never before the reading."""
    # Arrange
    monkeypatch.setattr(duration_module, "time_ns", lambda: READING_NS)

    # Act
    now = clock_microseconds_up()

    # Assert
    assert now == READING_NS // 1_000 + 1
