"""Tests for RateLimiter configuration paths."""

import pytest

from grelmicro.resilience import RateLimiter
from grelmicro.resilience.algorithms import GCRAConfig, TokenBucketConfig
from grelmicro.resilience.memory import MemoryRateLimiterBackend

LIMIT = 10
WINDOW = 60.0
CAPACITY = 5
REFILL_RATE = 1.0


@pytest.fixture
def _rate_limiter_backend() -> MemoryRateLimiterBackend:
    """Register a memory backend for the test."""
    return MemoryRateLimiterBackend()


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
def test_gcra_config() -> None:
    """`RateLimiter` accepts a `GCRAConfig` positional config."""
    rl = RateLimiter("auth", GCRAConfig(limit=LIMIT, window=WINDOW))
    assert rl.name == "auth"
    assert isinstance(rl.config, GCRAConfig)
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
def test_gcra_factory() -> None:
    """`RateLimiter.gcra` builds a GCRA rate limiter."""
    rl = RateLimiter.gcra("auth", limit=LIMIT, window=WINDOW)
    assert rl.name == "auth"
    assert isinstance(rl.config, GCRAConfig)
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
