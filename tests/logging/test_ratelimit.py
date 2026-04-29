"""Tests for the stdlib-logging RateLimitFilter."""

import logging
from collections.abc import Generator

import pytest
from pydantic import ValidationError

from grelmicro.log import RateLimitFilter, RateLimitFilterConfig

pytestmark = [pytest.mark.timeout(2)]

CAPACITY = 5
REFILL_RATE = 1.0


def _make_record(
    *,
    name: str = "grelmicro.test",
    level: int = logging.INFO,
    msg: str = "hello %s",
    args: tuple[str, ...] = ("world",),
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


# --- Construction & properties ---


def test_defaults() -> None:
    """Test default configuration."""
    # Arrange
    default_capacity = 5
    default_refill = 1.0
    default_cost = 1.0

    # Act
    flt = RateLimitFilter()

    # Assert
    assert flt.config.capacity == default_capacity
    assert flt.config.refill_rate == default_refill
    assert flt.config.key_mode == "logger"
    assert flt.config.cost == default_cost


def test_config_property_is_frozen() -> None:
    """Test config is an immutable Pydantic model."""
    # Act
    flt = RateLimitFilter(capacity=3, refill_rate=2)

    # Assert
    assert isinstance(flt.config, RateLimitFilterConfig)
    with pytest.raises(ValidationError):
        flt.config.capacity = 99  # type: ignore[misc]  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("capacity", "refill_rate", "cost"),
    [
        (0, 1, 1),
        (-1, 1, 1),
        (1, 0, 1),
        (1, -1, 1),
        (1, 1, 0),
        (1, 1, -1),
    ],
)
def test_invalid_config(capacity: int, refill_rate: float, cost: float) -> None:
    """Test non-positive values raise ValidationError."""
    # Act & Assert
    with pytest.raises(ValidationError, match="greater than"):
        RateLimitFilter(capacity=capacity, refill_rate=refill_rate, cost=cost)


# --- Filtering by default key mode (logger) ---


def test_filter_allows_within_capacity() -> None:
    """Test first `capacity` records for a logger are allowed."""
    # Arrange
    flt = RateLimitFilter(capacity=CAPACITY, refill_rate=REFILL_RATE)
    record = _make_record()

    # Act
    results = [flt.filter(record) for _ in range(CAPACITY)]

    # Assert
    assert all(results)


def test_filter_drops_beyond_capacity() -> None:
    """Test records past the burst are dropped."""
    # Arrange
    flt = RateLimitFilter(capacity=CAPACITY, refill_rate=REFILL_RATE)
    record = _make_record()
    for _ in range(CAPACITY):
        flt.filter(record)

    # Act
    result = flt.filter(record)

    # Assert
    assert result is False


def test_filter_per_logger_isolation() -> None:
    """Test different loggers do not share the bucket."""
    # Arrange
    flt = RateLimitFilter(capacity=2, refill_rate=REFILL_RATE)
    rec_a = _make_record(name="a")
    rec_b = _make_record(name="b")

    # Act: deplete logger "a"
    flt.filter(rec_a)
    flt.filter(rec_a)
    third_a = flt.filter(rec_a)
    first_b = flt.filter(rec_b)

    # Assert
    assert third_a is False
    assert first_b is True


# --- Key modes ---


def test_level_mode_shares_across_loggers_of_same_level() -> None:
    """Test key_mode='level' buckets records by log level."""
    # Arrange
    flt = RateLimitFilter(capacity=2, refill_rate=REFILL_RATE, key_mode="level")
    info_a = _make_record(name="a", level=logging.INFO)
    info_b = _make_record(name="b", level=logging.INFO)
    warn = _make_record(name="a", level=logging.WARNING)

    # Act
    flt.filter(info_a)
    flt.filter(info_b)
    third_info = flt.filter(info_a)
    first_warn = flt.filter(warn)

    # Assert
    assert third_info is False
    assert first_warn is True


def test_global_mode_shares_single_bucket() -> None:
    """Test key_mode='global' uses one bucket for everything."""
    # Arrange
    flt = RateLimitFilter(
        capacity=2, refill_rate=REFILL_RATE, key_mode="global"
    )
    rec_a = _make_record(name="a", level=logging.INFO)
    rec_b = _make_record(name="b", level=logging.WARNING)

    # Act
    flt.filter(rec_a)
    flt.filter(rec_b)
    third = flt.filter(rec_a)

    # Assert
    assert third is False


def test_template_mode_collapses_arg_values() -> None:
    """Test key_mode='template' shares the bucket across arg values."""
    # Arrange
    flt = RateLimitFilter(
        capacity=2, refill_rate=REFILL_RATE, key_mode="template"
    )
    a = _make_record(msg="user %s logged in", args=("alice",))
    b = _make_record(msg="user %s logged in", args=("bob",))
    c = _make_record(msg="user %s logged in", args=("carol",))

    # Act
    flt.filter(a)
    flt.filter(b)
    third = flt.filter(c)

    # Assert: same template, bucket is drained by third record
    assert third is False


def test_rendered_mode_buckets_per_unique_message() -> None:
    """Test key_mode='rendered' differentiates by rendered message."""
    # Arrange
    flt = RateLimitFilter(
        capacity=1, refill_rate=REFILL_RATE, key_mode="rendered"
    )
    a = _make_record(msg="user %s", args=("alice",))
    b = _make_record(msg="user %s", args=("bob",))

    # Act
    first_a = flt.filter(a)
    first_b = flt.filter(b)

    # Assert: different rendered messages, different buckets
    assert first_a is True
    assert first_b is True


def test_rendered_mode_falls_back_on_format_error() -> None:
    """Test rendered mode tolerates records that can't be formatted."""
    # Arrange
    flt = RateLimitFilter(capacity=2, refill_rate=1, key_mode="rendered")
    # Mismatched args trigger a TypeError inside getMessage().
    broken = _make_record(msg="need %s and %s", args=("only-one",))

    # Act (must not raise)
    allowed_1 = flt.filter(broken)
    allowed_2 = flt.filter(broken)
    dropped = flt.filter(broken)

    # Assert
    assert allowed_1 is True
    assert allowed_2 is True
    assert dropped is False


def test_custom_key_callable_overrides_mode() -> None:
    """Test `key=` callable wins over `key_mode`."""
    # Arrange
    calls: list[str] = []

    def my_key(record: logging.LogRecord) -> str:
        calls.append(record.name)
        return "constant-key"

    flt = RateLimitFilter(
        capacity=1, refill_rate=REFILL_RATE, key_mode="logger", key=my_key
    )
    rec_a = _make_record(name="a")
    rec_b = _make_record(name="b")

    # Act
    first = flt.filter(rec_a)
    second = flt.filter(rec_b)  # same constant-key, bucket empty

    # Assert
    assert first is True
    assert second is False
    assert calls == ["a", "b"]


# --- Cost ---


def test_cost_gt_one_drains_faster() -> None:
    """Test cost>1 drains the bucket proportionally faster."""
    # Arrange
    flt = RateLimitFilter(capacity=4, refill_rate=REFILL_RATE, cost=2)
    record = _make_record()

    # Act
    r1 = flt.filter(record)
    r2 = flt.filter(record)
    r3 = flt.filter(record)

    # Assert: 2 records consume the full capacity of 4.
    assert r1 is True
    assert r2 is True
    assert r3 is False


# --- reset ---


def test_reset_restores_key_capacity() -> None:
    """Test reset clears a single key."""
    # Arrange
    flt = RateLimitFilter(capacity=1, refill_rate=0.01)
    record = _make_record(name="target")
    flt.filter(record)
    assert flt.filter(record) is False  # empty

    # Act
    flt.reset("target")

    # Assert
    assert flt.filter(record) is True


def test_reset_only_affects_given_key() -> None:
    """Test reset of one key leaves others alone."""
    # Arrange
    flt = RateLimitFilter(capacity=1, refill_rate=0.01)
    rec_a = _make_record(name="a")
    rec_b = _make_record(name="b")
    flt.filter(rec_a)
    flt.filter(rec_b)

    # Act
    flt.reset("a")

    # Assert
    assert flt.filter(rec_a) is True
    assert flt.filter(rec_b) is False


# --- Integration with logging.Logger ---


@pytest.fixture
def rate_limited_logger() -> Generator[logging.Logger]:
    """Logger with a RateLimitFilter attached, cleaned up after."""
    logger = logging.getLogger("grelmicro.test.ratelimit_integration")
    logger.setLevel(logging.DEBUG)
    try:
        yield logger
    finally:
        for flt in list(logger.filters):
            logger.removeFilter(flt)


def test_attached_filter_drops_records(
    rate_limited_logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the filter integrates cleanly with logging.Logger."""
    # Arrange
    capacity = 3
    attempts = 5
    caplog.set_level(logging.DEBUG, logger=rate_limited_logger.name)
    flt = RateLimitFilter(capacity=capacity, refill_rate=0.01)
    rate_limited_logger.addFilter(flt)

    # Act
    for _ in range(attempts):
        rate_limited_logger.info("hello")

    # Assert
    assert len(caplog.records) == capacity
