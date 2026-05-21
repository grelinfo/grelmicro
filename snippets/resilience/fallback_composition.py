import httpx

from grelmicro.resilience import CircuitBreaker, Retry, fallback

breaker = CircuitBreaker("recs")
retrier = Retry.exponential("recs", when=httpx.HTTPError, attempts=3)


@fallback(when=Exception, default=[])
@retrier
@breaker
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
