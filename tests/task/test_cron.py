"""Test Cron Expression Parser."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from grelmicro.task._cron import CronExpression
from grelmicro.task.errors import CronError

UTC = ZoneInfo("UTC")

SEARCH_WINDOW = timedelta(days=5 * 366)
"""How far `next_after` and `previous_or_equal` search before giving up."""

LATE_FEB_29 = datetime(2104, 2, 29, tzinfo=UTC)
"""A Feb 29 eight years after the previous one, as 2100 is not a leap year."""

EARLY_FEB_29 = datetime(2096, 2, 29, tzinfo=UTC)
"""The Feb 29 before `LATE_FEB_29`."""


def dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Build a UTC datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_every_minute() -> None:
    """Test '* * * * *' fires the next minute."""
    expr = CronExpression("* * * * *")
    assert expr.next_after(dt(2026, 1, 1, 10, 30, 15)) == dt(2026, 1, 1, 10, 31)


def test_every_minute_truncates_seconds() -> None:
    """Test the result is truncated to whole minutes."""
    expr = CronExpression("* * * * *")
    result = expr.next_after(dt(2026, 1, 1, 10, 30, 0))
    assert result == dt(2026, 1, 1, 10, 31)
    assert result.second == 0
    assert result.microsecond == 0


def test_specific_time() -> None:
    """Test '30 2 * * *' fires at 02:30 daily."""
    expr = CronExpression("30 2 * * *")
    assert expr.next_after(dt(2026, 1, 1, 0, 0)) == dt(2026, 1, 1, 2, 30)
    # After today's fire, rolls to tomorrow.
    assert expr.next_after(dt(2026, 1, 1, 2, 30)) == dt(2026, 1, 2, 2, 30)


def test_step() -> None:
    """Test '*/15 * * * *' fires every 15 minutes."""
    expr = CronExpression("*/15 * * * *")
    assert expr.next_after(dt(2026, 1, 1, 10, 0)) == dt(2026, 1, 1, 10, 15)
    assert expr.next_after(dt(2026, 1, 1, 10, 16)) == dt(2026, 1, 1, 10, 30)
    assert expr.next_after(dt(2026, 1, 1, 10, 50)) == dt(2026, 1, 1, 11, 0)


def test_range_and_list() -> None:
    """Test '0 9-17 * * 1-5' fires on the hour, business hours, weekdays."""
    expr = CronExpression("0 9-17 * * 1-5")
    # 2026-01-05 is a Monday.
    assert expr.next_after(dt(2026, 1, 5, 8, 30)) == dt(2026, 1, 5, 9, 0)
    assert expr.next_after(dt(2026, 1, 5, 9, 0)) == dt(2026, 1, 5, 10, 0)
    assert expr.next_after(dt(2026, 1, 5, 17, 0)) == dt(2026, 1, 6, 9, 0)
    # 2026-01-09 is Friday 17:00 -> next is Monday 2026-01-12 09:00.
    assert expr.next_after(dt(2026, 1, 9, 17, 0)) == dt(2026, 1, 12, 9, 0)


def test_comma_list() -> None:
    """Test a comma list of minutes."""
    expr = CronExpression("0,15,45 * * * *")
    assert expr.next_after(dt(2026, 1, 1, 10, 0)) == dt(2026, 1, 1, 10, 15)
    assert expr.next_after(dt(2026, 1, 1, 10, 20)) == dt(2026, 1, 1, 10, 45)
    assert expr.next_after(dt(2026, 1, 1, 10, 45)) == dt(2026, 1, 1, 11, 0)


def test_month_rollover() -> None:
    """Test rollover into the next month."""
    expr = CronExpression("0 0 1 * *")
    assert expr.next_after(dt(2026, 1, 15)) == dt(2026, 2, 1)


def test_year_rollover() -> None:
    """Test rollover into the next year."""
    expr = CronExpression("0 0 1 1 *")
    assert expr.next_after(dt(2026, 6, 1)) == dt(2027, 1, 1)


def test_every_minute_truncates_microseconds() -> None:
    """A start with microseconds still yields a whole-minute result."""
    expr = CronExpression("* * * * *")
    start = datetime(2026, 1, 1, 10, 30, 15, 500, tzinfo=UTC)
    assert expr.next_after(start) == dt(2026, 1, 1, 10, 31)


@pytest.mark.parametrize(
    ("expr", "start", "expected"),
    [
        ("* * * 1 *", dt(2026, 12, 15, 10, 30), dt(2027, 1, 1)),
        ("* * * 4 *", dt(2026, 3, 15, 10, 30), dt(2026, 4, 1)),
        ("* * 15 * *", dt(2026, 2, 1, 10, 30), dt(2026, 2, 15)),
    ],
)
def test_next_after_skips_to_the_start_of_the_next_matching_day(
    expr: str, start: datetime, expected: datetime
) -> None:
    """A skipped month or day lands on midnight, not on the start's time."""
    assert CronExpression(expr).next_after(start) == expected


@pytest.mark.parametrize(
    ("expr", "start", "expected"),
    [
        ("59 23 * 3 *", dt(2026, 4, 10), dt(2026, 3, 31, 23, 59)),
        ("45 23 15 * *", dt(2026, 2, 20, 10, 30), dt(2026, 2, 15, 23, 45)),
    ],
)
def test_previous_or_equal_rewinds_to_the_end_of_the_previous_day(
    expr: str, start: datetime, expected: datetime
) -> None:
    """A skipped month or day rewinds to its last minute, 23:59."""
    assert CronExpression(expr).previous_or_equal(start) == expected


@pytest.mark.parametrize(
    ("expr", "start", "expected"),
    [
        ("5-5 * * * *", dt(2026, 1, 1, 10, 0), dt(2026, 1, 1, 10, 5)),
        ("*/1 * * * *", dt(2026, 1, 1, 10, 0), dt(2026, 1, 1, 10, 1)),
    ],
)
def test_single_value_range_and_unit_step(
    expr: str, start: datetime, expected: datetime
) -> None:
    """A range of one value and a step of one are valid fields."""
    assert CronExpression(expr).next_after(start) == expected


def test_next_after_searches_the_whole_window() -> None:
    """A match exactly at the end of the search window is found."""
    expr = CronExpression("0 0 29 2 *")
    start = LATE_FEB_29 - SEARCH_WINDOW - timedelta(minutes=1)
    assert expr.next_after(start) == LATE_FEB_29


def test_next_after_stops_at_the_end_of_the_window() -> None:
    """A match just past the search window raises."""
    expr = CronExpression("0 0 29 2 *")
    with pytest.raises(CronError, match="no matching time"):
        expr.next_after(LATE_FEB_29 - SEARCH_WINDOW - timedelta(days=2))


def test_previous_or_equal_searches_the_whole_window() -> None:
    """A match exactly at the start of the backward window is found."""
    expr = CronExpression("0 0 29 2 *")
    start = EARLY_FEB_29 + SEARCH_WINDOW
    assert expr.previous_or_equal(start) == EARLY_FEB_29


def test_previous_or_equal_stops_at_the_end_of_the_window() -> None:
    """A match just before the backward window is not returned."""
    expr = CronExpression("0 0 29 2 *")
    start = EARLY_FEB_29 + SEARCH_WINDOW + timedelta(days=2)
    assert expr.previous_or_equal(start) is None


def test_specific_month() -> None:
    """Test a specific month skips ahead."""
    expr = CronExpression("0 0 1 3 *")
    assert expr.next_after(dt(2026, 1, 10)) == dt(2026, 3, 1)


def test_dom_dow_or_semantics() -> None:
    """Test that restricting both dom and dow matches EITHER."""
    # Fire on the 15th of the month OR on any Monday.
    expr = CronExpression("0 0 15 * 1")
    # 2026-02-01 is Sunday. Next Monday is 2026-02-02.
    assert expr.next_after(dt(2026, 2, 1)) == dt(2026, 2, 2)
    # From 2026-02-03 (Tuesday), the 15th comes before next Monday (the 9th)?
    # 2026-02-09 is Monday, which is earlier than the 15th.
    assert expr.next_after(dt(2026, 2, 3)) == dt(2026, 2, 9)
    # From 2026-02-10, next is the 15th (Sunday) before next Monday (16th).
    assert expr.next_after(dt(2026, 2, 10)) == dt(2026, 2, 15)


def test_dom_only_restricted() -> None:
    """Test that only dom restricted ignores dow."""
    expr = CronExpression("0 0 15 * *")
    assert expr.next_after(dt(2026, 2, 1)) == dt(2026, 2, 15)


def test_dow_only_restricted() -> None:
    """Test that only dow restricted ignores dom."""
    # Every Sunday.
    expr = CronExpression("0 0 * * 0")
    # 2026-02-01 is Sunday at 00:00, next is the following Sunday.
    assert expr.next_after(dt(2026, 2, 1, 0, 0)) == dt(2026, 2, 8)


def test_sunday_seven_alias() -> None:
    """Test 7 is accepted as Sunday and matches the same days as 0."""
    expr_zero = CronExpression("0 0 * * 0")
    expr_seven = CronExpression("0 0 * * 7")
    start = dt(2026, 1, 1)
    assert expr_seven.next_after(start) == expr_zero.next_after(start)
    # 2026-01-04 is the first Sunday of 2026.
    assert expr_seven.next_after(start) == dt(2026, 1, 4)


def test_leap_year_feb_29() -> None:
    """Test Feb 29 only fires on leap years."""
    expr = CronExpression("0 0 29 2 *")
    # 2027 is not a leap year, next Feb 29 is 2028.
    assert expr.next_after(dt(2027, 1, 1)) == dt(2028, 2, 29)


def test_next_after_from_feb_29_does_not_crash() -> None:
    """A Feb 29 starting point must not crash the search-window bound."""
    expr = CronExpression("0 0 * * *")
    # The window bound must not land on a non-leap year (2028 + 5 = 2033).
    assert expr.next_after(dt(2028, 2, 29, 12)) == dt(2028, 3, 1)


def test_previous_or_equal_from_feb_29_does_not_crash() -> None:
    """A Feb 29 starting point must not crash the backward search bound."""
    expr = CronExpression("0 0 * * *")
    assert expr.previous_or_equal(dt(2028, 2, 29, 12)) == dt(2028, 2, 29)


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",  # too few fields
        "* * * * * *",  # too many fields
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # day of month below range
        "* * 32 * *",  # day of month above range
        "* * * 13 *",  # month out of range
        "* * * * 8",  # day of week out of range
        "*/0 * * * *",  # zero step
        "abc * * * *",  # non-numeric
        "5-2 * * * *",  # reversed range
        "1,,2 * * * *",  # empty list element
        "*/ * * * *",  # missing step value
        "*/x * * * *",  # non-numeric step
        "1-x * * * *",  # non-numeric range bound
        "5/2 * * * *",  # step without '*' or a range
    ],
)
def test_invalid_expression(expr: str) -> None:
    """Test malformed expressions raise CronError."""
    with pytest.raises(CronError):
        CronExpression(expr)


def test_repr_returns_source_expression() -> None:
    """`repr` echoes the source expression."""
    assert repr(CronExpression("*/15 9-17 * * 1-5")) == (
        "CronExpression('*/15 9-17 * * 1-5')"
    )


def test_previous_or_equal_at_match_returns_same_minute() -> None:
    """A start instant that matches is returned truncated to the minute."""
    expr = CronExpression("* * * * *")
    assert expr.previous_or_equal(dt(2026, 1, 1, 10, 30, 45)) == dt(
        2026, 1, 1, 10, 30
    )


def test_previous_or_equal_retreats_across_months() -> None:
    """The backward search skips a non-matching month to the prior month."""
    expr = CronExpression("0 0 1 3 *")
    # From April, the most recent March-1 fire is in the current year.
    assert expr.previous_or_equal(dt(2026, 4, 10)) == dt(2026, 3, 1)


def test_previous_or_equal_retreats_across_days_and_hours() -> None:
    """The backward search rewinds day and hour to the prior daily fire."""
    expr = CronExpression("0 2 * * *")
    # 01:00 is before today's 02:00 fire, so the match is yesterday 02:00.
    assert expr.previous_or_equal(dt(2026, 1, 5, 1, 0)) == dt(2026, 1, 4, 2, 0)


def test_previous_or_equal_returns_none_when_unreachable() -> None:
    """An impossible schedule yields `None` within the backward window."""
    expr = CronExpression("30 2 31 2 *")  # February never has 31 days
    assert expr.previous_or_equal(dt(2026, 6, 1)) is None


def test_cron_error_is_value_error() -> None:
    """Test CronError is a ValueError for broad except clauses."""
    with pytest.raises(ValueError, match="expected 5 fields"):
        CronExpression("* * *")


def test_impossible_date_raises() -> None:
    """Test a parseable but impossible date raises rather than looping."""
    # February never has 31 days.
    expr = CronExpression("30 2 31 2 *")
    with pytest.raises(CronError, match="no matching time"):
        expr.next_after(dt(2026, 1, 1))


def test_next_after_is_strict() -> None:
    """Test next_after returns a time strictly after the input."""
    expr = CronExpression("30 2 * * *")
    # Exactly at a fire time returns the next day, not the same instant.
    assert expr.next_after(dt(2026, 1, 1, 2, 30)) == dt(2026, 1, 2, 2, 30)


def test_preserves_tzinfo() -> None:
    """Test the result keeps the input timezone."""
    zurich = ZoneInfo("Europe/Zurich")
    expr = CronExpression("0 2 * * *")
    start = datetime(2026, 6, 1, 0, 0, tzinfo=zurich)
    result = expr.next_after(start)
    assert result.tzinfo is zurich
    assert result == datetime(2026, 6, 1, 2, 0, tzinfo=zurich)
