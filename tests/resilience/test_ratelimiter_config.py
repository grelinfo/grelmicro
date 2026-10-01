"""Tests for RateLimiter configuration paths."""

from datetime import timedelta

import pytest
from pydantic import TypeAdapter

from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter import (
    RateLimiterConfig,
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

LIMIT = 10
WINDOW = 60
CAPACITY = 5
REFILL_RATE = 1.0


@pytest.fixture
def _rate_limiter_backend() -> MemoryRateLimiterAdapter:
    """Register a memory backend for the test."""
    return MemoryRateLimiterAdapter()


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_token_bucket_config() -> None:
    """`RateLimiter` accepts a `TokenBucketConfig` positional config."""
    rl = RateLimiter.from_config(
        "api", TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE)
    )
    assert rl.name == "api"
    assert isinstance(rl.config, TokenBucketConfig)
    assert rl.config.capacity == CAPACITY
    assert rl.config.refill_rate == REFILL_RATE


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_sliding_window_config() -> None:
    """`RateLimiter` accepts a `SlidingWindowConfig` positional config."""
    rl = RateLimiter.from_config(
        "auth", SlidingWindowConfig(limit=LIMIT, window=WINDOW)
    )
    assert rl.name == "auth"
    assert isinstance(rl.config, SlidingWindowConfig)
    assert rl.config.limit == LIMIT
    assert rl.config.window == WINDOW


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_fail_open_in_config() -> None:
    """`fail_open` set on the algorithm config flows to the rate limiter."""
    cfg = TokenBucketConfig(
        capacity=CAPACITY, refill_rate=REFILL_RATE, fail_open=True
    )
    rl = RateLimiter.from_config("api", cfg)
    assert rl.config.fail_open is True


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_token_bucket_factory() -> None:
    """`RateLimiter.token_bucket` builds a token-bucket rate limiter."""
    rl = RateLimiter.token_bucket(
        "api", capacity=CAPACITY, refill_rate=REFILL_RATE
    )
    assert rl.name == "api"
    assert isinstance(rl.config, TokenBucketConfig)
    assert rl.config.capacity == CAPACITY
    assert rl.config.refill_rate == REFILL_RATE
    assert rl.config.fail_open is False


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_sliding_window_factory() -> None:
    """`RateLimiter.sliding_window` builds a sliding-window rate limiter."""
    rl = RateLimiter.sliding_window("auth", limit=LIMIT, window=WINDOW)
    assert rl.name == "auth"
    assert isinstance(rl.config, SlidingWindowConfig)
    assert rl.config.limit == LIMIT
    assert rl.config.window == WINDOW
    assert rl.config.fail_open is False


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_factory_passes_fail_open() -> None:
    """Factory classmethods forward `fail_open` into the built config."""
    rl = RateLimiter.token_bucket(
        "api", capacity=CAPACITY, refill_rate=REFILL_RATE, fail_open=True
    )
    assert rl.config.fail_open is True


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_from_config_classmethod() -> None:
    """`RateLimiter.from_config` mirrors the positional constructor."""
    cfg = TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE)
    rl = RateLimiter.from_config("api", cfg)
    assert rl.name == "api"
    assert rl.config is cfg


def test_discriminator_values() -> None:
    """Discriminator values are part of the public serialized API surface."""
    assert (
        TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE).kind
        == "token_bucket"
    )
    assert (
        SlidingWindowConfig(limit=LIMIT, window=WINDOW).kind == "sliding_window"
    )


def test_rate_limiter_config_union_round_trips() -> None:
    """`RateLimiterConfig` parses both discriminator values."""
    adapter = TypeAdapter(RateLimiterConfig)
    sliding = adapter.validate_python(
        {"kind": "sliding_window", "limit": LIMIT, "window": WINDOW}
    )
    bucket = adapter.validate_python(
        {
            "kind": "token_bucket",
            "capacity": CAPACITY,
            "refill_rate": REFILL_RATE,
        }
    )
    assert isinstance(sliding, SlidingWindowConfig)
    assert isinstance(bucket, TokenBucketConfig)


def test_bare_constructor_names_the_three_doors() -> None:
    """`RateLimiter(...)` refuses and points at the doors that work."""
    with pytest.raises(TypeError, match="no default algorithm") as excinfo:
        RateLimiter("api", TokenBucketConfig(capacity=1, refill_rate=1))
    message = str(excinfo.value)
    assert "token_bucket" in message
    assert "sliding_window" in message
    assert "from_config" in message


@pytest.mark.parametrize("window", [1.5, 60.0, True])
def test_sliding_window_refuses_a_float_window(window: object) -> None:
    """A window is whole seconds or a timedelta, never a float."""
    with pytest.raises(ValueError, match="whole seconds or a timedelta"):
        SlidingWindowConfig(limit=LIMIT, window=window)


@pytest.mark.parametrize("window", [0, -1, timedelta(0), timedelta(seconds=-1)])
def test_sliding_window_refuses_a_window_that_is_not_positive(
    window: int | timedelta,
) -> None:
    """A window of zero or less is refused."""
    with pytest.raises(ValueError, match="greater than zero"):
        SlidingWindowConfig(limit=LIMIT, window=window)


def test_sliding_window_takes_a_timedelta_under_a_second() -> None:
    """A sub-second window is a timedelta."""
    config = SlidingWindowConfig(
        limit=LIMIT, window=timedelta(milliseconds=500)
    )

    assert config.window == timedelta(milliseconds=500)


@pytest.mark.parametrize(
    ("raw", "window"),
    [("60", 60), ("PT0.5S", timedelta(milliseconds=500))],
)
def test_sliding_window_reads_a_window_from_text(
    raw: str, window: int | timedelta
) -> None:
    """Text from the environment is whole seconds or an ISO 8601 duration."""
    config = SlidingWindowConfig.model_validate({"limit": LIMIT, "window": raw})

    assert config.window == window


def test_sliding_window_slot_under_a_microsecond_is_refused() -> None:
    """A window that gives each request less than a microsecond is refused."""
    with pytest.raises(ValueError, match="at least one microsecond"):
        SlidingWindowConfig(limit=10, window=timedelta(microseconds=9))


def test_sliding_window_slot_of_a_microsecond_is_accepted() -> None:
    """A window that gives each request one microsecond is accepted."""
    config = SlidingWindowConfig(limit=10, window=timedelta(microseconds=10))

    assert config.limit == LIMIT
