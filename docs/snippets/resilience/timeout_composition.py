import httpx

from grelmicro.resilience import CircuitBreaker, Retry, Timeout, fallback

breaker = CircuitBreaker("recs")
retrier = Retry.exponential("recs", when=httpx.HTTPError, attempts=3)
call_timeout = Timeout("recs", seconds=1.0)


@fallback(when=Exception, default=[])
@retrier
@breaker
@call_timeout
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
