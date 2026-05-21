import httpx

from grelmicro.resilience import falling_back


async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    async with falling_back(when=httpx.HTTPError, default=[]) as result:
        response = await client.get(f"/recs/{user_id}")
        response.raise_for_status()
        result.set(response.json())
    return result.value
