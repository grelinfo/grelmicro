"""Tests for the RateLimiter configuration paths."""

import pytest

from grelmicro.resilience import RateLimiter
from grelmicro.resilience.algorithms import GCRA, TokenBucket
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.ratelimiter import RateLimiterConfig

LIMIT = 10
WINDOW = 60.0
CAPACITY = 5
REFILL_RATE = 1.0


@pytest.fixture
def _sync_backend() -> MemoryRateLimiterBackend:
    """Register a memory backend for the test."""
    return MemoryRateLimiterBackend()


@pytest.mark.usefixtures("_sync_backend")
def test_programmatic_path() -> None:
    """Plain kwargs build a RateLimiter directly."""
    rl = RateLimiter(
        "api", algorithm=TokenBucket(capacity=CAPACITY, refill_rate=REFILL_RATE)
    )
    assert rl.name == "api"
    assert isinstance(rl.config.algorithm, TokenBucket)


@pytest.mark.usefixtures("_sync_backend")
def test_declarative_path_uses_from_config() -> None:
    """`RateLimiter.from_config()` constructs from a name and a config."""
    cfg = RateLimiterConfig(algorithm=GCRA(limit=LIMIT, window=WINDOW))
    rl = RateLimiter.from_config("auth", cfg)
    assert rl.name == "auth"
    assert rl.config is cfg


@pytest.mark.usefixtures("_sync_backend")
def test_from_config_passes_fail_open() -> None:
    """`fail_open=` is honoured on the declarative path."""
    cfg = RateLimiterConfig(
        algorithm=TokenBucket(capacity=CAPACITY, refill_rate=REFILL_RATE)
    )
    rl = RateLimiter.from_config("api", cfg, fail_open=True)
    assert rl._fail_open is True
