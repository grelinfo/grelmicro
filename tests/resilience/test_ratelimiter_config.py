"""Tests for RateLimiter configuration paths."""

import pytest
from pydantic import TypeAdapter

from grelmicro.resilience import RateLimiter
from grelmicro.resilience.algorithms import (
    RateLimiterConfig,
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.memory import MemoryRateLimiterAdapter

LIMIT = 10
WINDOW = 60.0
CAPACITY = 5
REFILL_RATE = 1.0


@pytest.fixture
def _rate_limiter_backend() -> MemoryRateLimiterAdapter:
    """Register a memory backend for the test."""
    return MemoryRateLimiterAdapter()


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_token_bucket_config() -> None:
    """`RateLimiter` accepts a `TokenBucketConfig` positional config."""
    rl = RateLimiter(
        "api", TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE)
    )
    assert rl.name == "api"
    assert isinstance(rl.config, TokenBucketConfig)
    assert rl.config.capacity == CAPACITY
    assert rl.config.refill_rate == REFILL_RATE


@pytest.mark.usefixtures("_rate_limiter_backend")
def test_sliding_window_config() -> None:
    """`RateLimiter` accepts a `SlidingWindowConfig` positional config."""
    rl = RateLimiter("auth", SlidingWindowConfig(limit=LIMIT, window=WINDOW))
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
    rl = RateLimiter("api", cfg)
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
        TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE).type
        == "token_bucket"
    )
    assert (
        SlidingWindowConfig(limit=LIMIT, window=WINDOW).type == "sliding_window"
    )


def test_rate_limiter_config_union_round_trips() -> None:
    """`RateLimiterConfig` parses both discriminator values."""
    adapter = TypeAdapter(RateLimiterConfig)
    sliding = adapter.validate_python(
        {"type": "sliding_window", "limit": LIMIT, "window": WINDOW}
    )
    bucket = adapter.validate_python(
        {
            "type": "token_bucket",
            "capacity": CAPACITY,
            "refill_rate": REFILL_RATE,
        }
    )
    assert isinstance(sliding, SlidingWindowConfig)
    assert isinstance(bucket, TokenBucketConfig)
