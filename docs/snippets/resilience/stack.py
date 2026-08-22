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

SYSTEM = "recs-api"
CALL = "recs-list"

# Named for the system, so every call site shares one circuit and one budget.
breaker = CircuitBreaker(SYSTEM)
limiter = RateLimiter.token_bucket(SYSTEM, capacity=20, refill_rate=10)
pool = Bulkhead(SYSTEM, max_concurrent=10)

recs_list = Stack(
    CALL,
    patterns=[
        # Named for this call: its default and its deadline are its own.
        Fallback(CALL, when=Exception, default=[]),
        Retry.exponential(CALL, when=httpx.HTTPError, attempts=3),
        breaker,
        limiter(key="user:{user_id}"),
        pool,
        Timeout(CALL, seconds=1.0),
    ],
)


@recs_list
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[str]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
