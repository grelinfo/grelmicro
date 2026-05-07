import httpx

from grelmicro.resilience import retry


@retry(on=httpx.HTTPError, attempts=3)
async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


async def main() -> bytes:
    async with httpx.AsyncClient() as client:
        return await fetch(client, "https://example.com")
