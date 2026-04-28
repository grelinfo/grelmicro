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
def _sync_backend() -> MemoryRateLimiterBackend:
    """Register a memory backend for the test."""
    return MemoryRateLimiterBackend()


@pytest.mark.usefixtures("_sync_backend")
def test_token_bucket_config() -> None:
    """`RateLimiter` accepts a `TokenBucketConfig` positional config."""
    rl = RateLimiter(
        "api", TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE)
    )
    assert rl.name == "api"
    assert isinstance(rl.config, TokenBucketConfig)
    assert rl.config.capacity == CAPACITY
    assert rl.config.refill_rate == REFILL_RATE


@pytest.mark.usefixtures("_sync_backend")
def test_gcra_config() -> None:
    """`RateLimiter` accepts a `GCRAConfig` positional config."""
    rl = RateLimiter("auth", GCRAConfig(limit=LIMIT, window=WINDOW))
    assert rl.name == "auth"
    assert isinstance(rl.config, GCRAConfig)
    assert rl.config.limit == LIMIT
    assert rl.config.window == WINDOW


@pytest.mark.usefixtures("_sync_backend")
def test_fail_open_in_config() -> None:
    """`fail_open` set on the algorithm config flows to the rate limiter."""
    cfg = TokenBucketConfig(
        capacity=CAPACITY, refill_rate=REFILL_RATE, fail_open=True
    )
    rl = RateLimiter("api", cfg)
    assert rl._fail_open is True
