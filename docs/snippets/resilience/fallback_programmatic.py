import httpx

from grelmicro.resilience import Fallback

policy = Fallback("recs", when=httpx.HTTPError, default=[])


@policy
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
