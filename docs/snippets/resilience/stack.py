import httpx

from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    RateLimiter,
    Retry,
    Stack,
    Timeout,
)

limiter = RateLimiter.token_bucket("recs", capacity=20, refill_rate=10)

recs = Stack(
    "recs",
    patterns=[
        Fallback("recs", when=Exception, default=[]),
        Retry.exponential("recs", when=httpx.HTTPError, attempts=3),
        CircuitBreaker("recs"),
        limiter(key="user:{user_id}"),
        Bulkhead("recs", max_concurrent=10),
        Timeout("recs", seconds=1.0),
    ],
)


@recs
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[str]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
