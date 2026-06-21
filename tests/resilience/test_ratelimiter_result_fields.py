"""Exact result-field tests for the in-memory rate limiter strategies.

The broader suite asserts `allowed` and `remaining`. These tests pin the
exact `limit`, `retry_after`, and `reset_after` values for the token bucket
and GCRA strategies, in both allowed and denied states, driven by a
`VirtualClock` so the timing math is deterministic. A non-unit `refill_rate`
and a non-zero clock start keep the formulas sensitive to sign and operator.
"""

from collections.abc import AsyncGenerator

import pytest

from grelmicro import Grelmicro
from grelmicro.clock import VirtualClock
from grelmicro.resilience import RateLimiters
from grelmicro.resilience.ratelimiter import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

# capacity / refill chosen so `* refill_rate` and `/ refill_rate` differ.
TB_CAPACITY = 10
TB_REFILL_RATE = 2.0
# window / limit give an emission interval of 2.0 seconds per token.
SW_LIMIT = 5
SW_WINDOW = 10.0
SW_EMISSION = SW_WINDOW / SW_LIMIT
# Non-zero start so `x - now` and `x + now` mutations diverge.
CLOCK_START = 1000.0


@pytest.fixture
async def clock() -> AsyncGenerator[VirtualClock]:
    """Virtual clock plus an open in-memory rate limiter backend."""
    virtual_clock = VirtualClock(start=CLOCK_START)
    backend = MemoryRateLimiterAdapter()
    async with Grelmicro(uses=[virtual_clock, RateLimiters(backend)]):
        yield virtual_clock


def token_bucket(name: str) -> RateLimiter:
    """Build a registered token-bucket limiter."""
    return RateLimiter.token_bucket(
        name, capacity=TB_CAPACITY, refill_rate=TB_REFILL_RATE
    )


def sliding_window(name: str) -> RateLimiter:
    """Build a registered sliding-window (GCRA) limiter."""
    return RateLimiter.sliding_window(name, limit=SW_LIMIT, window=SW_WINDOW)


# --- Token bucket ---------------------------------------------------------


@pytest.mark.usefixtures("clock")
async def test_token_bucket_acquire_allowed_fields() -> None:
    """Allowed acquire reports exact limit, remaining, and reset_after."""
    rl = token_bucket("tb-allowed")

    result = await rl.acquire(cost=1)

    tokens_left = TB_CAPACITY - 1
    expected_reset = (TB_CAPACITY - tokens_left) / TB_REFILL_RATE
    assert result.allowed is True
    assert result.limit == TB_CAPACITY
    assert result.remaining == tokens_left
    assert result.retry_after == 0.0
    assert result.reset_after == expected_reset


@pytest.mark.usefixtures("clock")
async def test_token_bucket_acquire_denied_fields() -> None:
    """Denied acquire reports exact retry_after and reset_after."""
    rl = token_bucket("tb-denied")
    tokens_left = 1
    cost = 3

    await rl.acquire(cost=TB_CAPACITY - tokens_left)  # leaves 1 token
    result = await rl.acquire(cost=cost)  # 1 < 3, denied, nothing deducted

    expected_retry = (cost - tokens_left) / TB_REFILL_RATE
    expected_reset = (TB_CAPACITY - tokens_left) / TB_REFILL_RATE
    assert result.allowed is False
    assert result.limit == TB_CAPACITY
    assert result.remaining == tokens_left
    assert result.retry_after == expected_retry
    assert result.reset_after == expected_reset


@pytest.mark.usefixtures("clock")
async def test_token_bucket_denied_preserves_key_state() -> None:
    """A denied acquire keeps usable per-key state for the next call."""
    rl = token_bucket("tb-denied-state")

    await rl.acquire(cost=9)  # leaves 1 token
    assert (await rl.acquire(cost=3)).allowed is False  # denied
    # The denied call must not corrupt the bucket: a follow-up peek
    # still reads the retained token, rather than crashing on None.
    follow_up = await rl.peek()

    assert follow_up.allowed is True
    assert follow_up.remaining == 1


async def test_token_bucket_refill_is_rate_times_elapsed(
    clock: VirtualClock,
) -> None:
    """Refill adds refill_rate * elapsed tokens, not elapsed / refill_rate."""
    rl = token_bucket("tb-refill")
    elapsed = 1.0

    await rl.acquire(cost=TB_CAPACITY)  # drains the bucket to 0
    await clock.advance(elapsed)
    result = await rl.peek()

    refilled = int(elapsed * TB_REFILL_RATE)
    expected_reset = (TB_CAPACITY - refilled) / TB_REFILL_RATE
    assert result.allowed is True
    assert result.remaining == refilled
    assert result.reset_after == expected_reset


@pytest.mark.usefixtures("clock")
async def test_token_bucket_peek_allowed_at_one_token() -> None:
    """One whole token is enough for peek to report allowed."""
    rl = token_bucket("tb-peek-boundary")

    await rl.acquire(cost=TB_CAPACITY - 1)  # leaves exactly 1.0 token
    result = await rl.peek()

    assert result.allowed is True
    assert result.remaining == 1
    assert result.retry_after == 0.0


async def test_token_bucket_peek_denied_fields(clock: VirtualClock) -> None:
    """Denied peek with a fractional token reports exact retry/reset."""
    rl = token_bucket("tb-peek-denied")
    elapsed = 0.25

    await rl.acquire(cost=TB_CAPACITY)  # drains to 0
    await clock.advance(elapsed)  # refills a fractional token
    result = await rl.peek()

    tokens = elapsed * TB_REFILL_RATE
    expected_retry = (1.0 - tokens) / TB_REFILL_RATE
    expected_reset = (TB_CAPACITY - tokens) / TB_REFILL_RATE
    assert result.allowed is False
    assert result.limit == TB_CAPACITY
    assert result.remaining == 0
    assert result.retry_after == expected_retry
    assert result.reset_after == expected_reset


# --- Sliding window (GCRA) ------------------------------------------------


@pytest.mark.usefixtures("clock")
async def test_sliding_window_acquire_allowed_fields() -> None:
    """Allowed acquire reports exact limit, remaining, and reset_after."""
    rl = sliding_window("sw-allowed")

    result = await rl.acquire(cost=1)

    assert result.allowed is True
    assert result.limit == SW_LIMIT
    assert result.remaining == SW_LIMIT - 1
    assert result.retry_after == 0.0
    # new_tat - now = (now + emission) - now = emission
    assert result.reset_after == SW_EMISSION


@pytest.mark.usefixtures("clock")
async def test_sliding_window_acquire_denied_fields() -> None:
    """Denied acquire reports exact retry_after and reset_after."""
    rl = sliding_window("sw-denied")

    for _ in range(SW_LIMIT):
        assert (await rl.acquire(cost=1)).allowed is True
    result = await rl.acquire(cost=1)  # over the limit, denied

    expected_reset = SW_LIMIT * SW_EMISSION
    assert result.allowed is False
    assert result.limit == SW_LIMIT
    assert result.remaining == 0
    assert result.retry_after == SW_EMISSION
    assert result.reset_after == expected_reset


@pytest.mark.usefixtures("clock")
async def test_sliding_window_peek_allowed_fields() -> None:
    """Fresh peek reports a zero reset_after, exercising the clamp."""
    rl = sliding_window("sw-peek-allowed")

    result = await rl.peek()

    assert result.allowed is True
    assert result.limit == SW_LIMIT
    assert result.remaining == SW_LIMIT
    assert result.retry_after == 0.0
    # max(0.0, new_tat - now) with new_tat == now
    assert result.reset_after == 0.0


async def test_sliding_window_peek_denied_fields(
    clock: VirtualClock,
) -> None:
    """Denied peek reports exact retry_after and reset_after."""
    rl = sliding_window("sw-peek-denied")
    elapsed = 0.5

    for _ in range(SW_LIMIT):
        await rl.acquire(cost=1)  # drives tat to now + limit * emission
    await clock.advance(elapsed)
    result = await rl.peek()

    expected_retry = SW_EMISSION - elapsed
    expected_reset = SW_LIMIT * SW_EMISSION - elapsed
    assert result.allowed is False
    assert result.limit == SW_LIMIT
    assert result.remaining == 0
    assert result.retry_after == expected_retry
    assert result.reset_after == expected_reset
