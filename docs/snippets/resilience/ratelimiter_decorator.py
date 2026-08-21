import httpx

from grelmicro.resilience import RateLimiter

limiter = RateLimiter.token_bucket("partner", capacity=20, refill_rate=10)


@limiter
async def list_products(client: httpx.AsyncClient) -> list[str]:
    response = await client.get("/products")
    return response.json()


@limiter(key="user:{user_id}", max_wait=2.0)
async def get_profile(
    client: httpx.AsyncClient, user_id: str
) -> dict[str, str]:
    response = await client.get(f"/profile/{user_id}")
    return response.json()
