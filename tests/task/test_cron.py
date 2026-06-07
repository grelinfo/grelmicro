"""Test Cron Expression Parser."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from grelmicro.task._cron import CronExpression
from grelmicro.task.errors import CronError

UTC = ZoneInfo("UTC")


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
    ],
)
def test_invalid_expression(expr: str) -> None:
    """Test malformed expressions raise CronError."""
    with pytest.raises(CronError):
        CronExpression(expr)


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
