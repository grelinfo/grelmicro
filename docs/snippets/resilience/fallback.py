import httpx

from grelmicro.resilience import fallback


@fallback(when=httpx.HTTPError, default=[])
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
