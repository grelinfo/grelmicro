import httpx

from grelmicro.resilience import fallback


def _cached_recommendations(exc: BaseException) -> list[dict]:
    # Read from a local cache shared by the process.
    return []


@fallback(when=httpx.HTTPError, factory=_cached_recommendations)
async def get_recommendations(
    client: httpx.AsyncClient, user_id: str
) -> list[dict]:
    response = await client.get(f"/recs/{user_id}")
    response.raise_for_status()
    return response.json()
