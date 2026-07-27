"""Type assertions for the resilience primitives."""

from typing import assert_type

from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    RateLimiter,
    Retry,
    Shield,
    Timeout,
    retry,
)


async def fetch(order_id: str, *, retries: int = 3) -> dict[str, int]:
    """Fetch an order, pinning decorator behavior."""
    return {order_id: retries}


# --- Factories return the concrete policy, not a base or `Any` ---

assert_type(Retry.exponential("api", when=ValueError), Retry)
assert_type(Retry.constant("poll", when=ValueError), Retry)
assert_type(CircuitBreaker.consecutive_count("api"), CircuitBreaker)
assert_type(Shield.api("api"), Shield)
assert_type(Shield.internal("internal"), Shield)
assert_type(Shield.slow("slow"), Shield)
assert_type(
    RateLimiter.token_bucket("rl", capacity=10, refill_rate=1.0), RateLimiter
)
assert_type(RateLimiter.sliding_window("rl", limit=10, window=1.0), RateLimiter)


# --- Decorating preserves the wrapped signature ---

retried = Retry.exponential("api", when=ValueError)(fetch)
shielded = Shield.api("api")(fetch)
bounded = Bulkhead("pool", max_concurrent=4)(fetch)
deadlined = Timeout("slow", seconds=1.0)(fetch)
broken = CircuitBreaker.consecutive_count("api")(fetch)
fell_back = Fallback("api", when=ValueError, default={})(fetch)


# The module-level functional form preserves signatures too.
functional = retry(when=ValueError)(fetch)


async def call_decorated() -> None:
    """Awaiting a decorated coroutine yields the original return type."""
    assert_type(await retried("ORD-1"), dict[str, int])
    assert_type(await shielded("ORD-1", retries=5), dict[str, int])
    assert_type(await bounded("ORD-1"), dict[str, int])
    assert_type(await deadlined("ORD-1"), dict[str, int])
    assert_type(await broken("ORD-1"), dict[str, int])
    assert_type(await fell_back("ORD-1"), dict[str, int])
    assert_type(await functional("ORD-1"), dict[str, int])
