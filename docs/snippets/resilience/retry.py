import httpx

from grelmicro.resilience import retry


@retry(on=httpx.HTTPError, attempts=3)
async def fetch(url: str) -> bytes:
    response = await httpx.AsyncClient().get(url)
    response.raise_for_status()
    return response.content
