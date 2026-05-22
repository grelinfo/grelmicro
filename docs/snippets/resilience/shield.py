import httpx

from grelmicro.resilience import shield


@shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content
