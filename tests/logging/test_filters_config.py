"""Tests for the three-paths logging filter construction."""

import pytest

from grelmicro.log._dedup import DuplicateFilter, DuplicateFilterConfig
from grelmicro.log._ratelimit import (
    RateLimitFilter,
    RateLimitFilterConfig,
)

# RateLimitFilter constants
RL_CAPACITY_KWARG = 10
RL_REFILL_RATE_KWARG = 2.0
RL_CAPACITY_ENV = 50
RL_REFILL_RATE_ENV = 5.0
RL_DEFAULT_CAPACITY = 5
RL_DEFAULT_REFILL_RATE = 1.0

# DuplicateFilter constants
DF_REPS_KWARG = 3
DF_CACHE_KWARG = 50
DF_REPS_ENV = 9
DF_CACHE_ENV = 200
DF_DEFAULT_REPS = 5
DF_DEFAULT_CACHE = 100


# --- RateLimitFilter ---


def test_rate_limit_programmatic_path() -> None:
    """Plain kwargs build a config, falling back to defaults."""
    flt = RateLimitFilter(
        capacity=RL_CAPACITY_KWARG, refill_rate=RL_REFILL_RATE_KWARG
    )
    assert flt.config.capacity == RL_CAPACITY_KWARG
    assert flt.config.refill_rate == RL_REFILL_RATE_KWARG


def test_rate_limit_declarative_path() -> None:
    """`RateLimitFilter.from_config()` constructs from a pre-built config."""
    cfg = RateLimitFilterConfig(
        capacity=RL_CAPACITY_KWARG, refill_rate=RL_REFILL_RATE_KWARG
    )
    flt = RateLimitFilter.from_config(cfg)
    assert flt.config is cfg


def test_rate_limit_from_config_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RateLimitFilter.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_RATE_LIMIT_FILTER_CAPACITY", str(RL_CAPACITY_ENV))
    cfg = RateLimitFilterConfig(
        capacity=RL_CAPACITY_KWARG, refill_rate=RL_REFILL_RATE_KWARG
    )
    flt = RateLimitFilter.from_config(cfg)
    assert flt.config.capacity == RL_CAPACITY_KWARG


def test_rate_limit_environmental_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_RATE_LIMIT_FILTER_*`` populate unset fields."""
    monkeypatch.setenv("GREL_RATE_LIMIT_FILTER_CAPACITY", str(RL_CAPACITY_ENV))
    monkeypatch.setenv(
        "GREL_RATE_LIMIT_FILTER_REFILL_RATE", str(RL_REFILL_RATE_ENV)
    )
    flt = RateLimitFilter()
    assert flt.config.capacity == RL_CAPACITY_ENV
    assert flt.config.refill_rate == RL_REFILL_RATE_ENV


def test_rate_limit_kwargs_override_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv("GREL_RATE_LIMIT_FILTER_CAPACITY", str(RL_CAPACITY_ENV))
    flt = RateLimitFilter(capacity=RL_CAPACITY_KWARG)
    assert flt.config.capacity == RL_CAPACITY_KWARG


def test_rate_limit_env_prefix_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived prefix."""
    monkeypatch.setenv("MYAPP_RATE_LIMIT_FILTER_CAPACITY", str(RL_CAPACITY_ENV))
    flt = RateLimitFilter(env_prefix="MYAPP_RATE_LIMIT_FILTER_")
    assert flt.config.capacity == RL_CAPACITY_ENV


def test_rate_limit_read_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv("GREL_RATE_LIMIT_FILTER_CAPACITY", str(RL_CAPACITY_ENV))
    flt = RateLimitFilter(read_env=False)
    assert flt.config.capacity == RL_DEFAULT_CAPACITY


def test_rate_limit_zero_config_uses_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, RateLimitFilterConfig defaults take over."""
    monkeypatch.delenv("GREL_RATE_LIMIT_FILTER_CAPACITY", raising=False)
    monkeypatch.delenv("GREL_RATE_LIMIT_FILTER_REFILL_RATE", raising=False)
    flt = RateLimitFilter()
    assert flt.config.capacity == RL_DEFAULT_CAPACITY
    assert flt.config.refill_rate == RL_DEFAULT_REFILL_RATE


# --- DuplicateFilter ---


def test_duplicate_programmatic_path() -> None:
    """Plain kwargs build a config, falling back to defaults."""
    flt = DuplicateFilter(
        allowed_repetitions=DF_REPS_KWARG, cache_size=DF_CACHE_KWARG
    )
    assert flt.config.allowed_repetitions == DF_REPS_KWARG
    assert flt.config.cache_size == DF_CACHE_KWARG


def test_duplicate_declarative_path() -> None:
    """`DuplicateFilter.from_config()` constructs from a pre-built config."""
    cfg = DuplicateFilterConfig(
        allowed_repetitions=DF_REPS_KWARG, cache_size=DF_CACHE_KWARG
    )
    flt = DuplicateFilter.from_config(cfg)
    assert flt.config is cfg


def test_duplicate_from_config_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DuplicateFilter.from_config()` ignores env even when set."""
    monkeypatch.setenv(
        "GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS", str(DF_REPS_ENV)
    )
    cfg = DuplicateFilterConfig(allowed_repetitions=DF_REPS_KWARG)
    flt = DuplicateFilter.from_config(cfg)
    assert flt.config.allowed_repetitions == DF_REPS_KWARG


def test_duplicate_environmental_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_DUPLICATE_FILTER_*`` populate unset fields."""
    monkeypatch.setenv(
        "GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS", str(DF_REPS_ENV)
    )
    monkeypatch.setenv("GREL_DUPLICATE_FILTER_CACHE_SIZE", str(DF_CACHE_ENV))
    flt = DuplicateFilter()
    assert flt.config.allowed_repetitions == DF_REPS_ENV
    assert flt.config.cache_size == DF_CACHE_ENV


def test_duplicate_kwargs_override_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv(
        "GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS", str(DF_REPS_ENV)
    )
    flt = DuplicateFilter(allowed_repetitions=DF_REPS_KWARG)
    assert flt.config.allowed_repetitions == DF_REPS_KWARG


def test_duplicate_env_prefix_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_prefix=`` replaces the auto-derived prefix."""
    monkeypatch.setenv(
        "MYAPP_DUPLICATE_FILTER_ALLOWED_REPETITIONS", str(DF_REPS_ENV)
    )
    flt = DuplicateFilter(env_prefix="MYAPP_DUPLICATE_FILTER_")
    assert flt.config.allowed_repetitions == DF_REPS_ENV


def test_duplicate_read_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``read_env=False`` skips env reads entirely."""
    monkeypatch.setenv(
        "GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS", str(DF_REPS_ENV)
    )
    flt = DuplicateFilter(read_env=False)
    assert flt.config.allowed_repetitions == DF_DEFAULT_REPS


def test_duplicate_zero_config_uses_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, DuplicateFilterConfig defaults take over."""
    monkeypatch.delenv(
        "GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS", raising=False
    )
    monkeypatch.delenv("GREL_DUPLICATE_FILTER_CACHE_SIZE", raising=False)
    flt = DuplicateFilter()
    assert flt.config.allowed_repetitions == DF_DEFAULT_REPS
    assert flt.config.cache_size == DF_DEFAULT_CACHE
