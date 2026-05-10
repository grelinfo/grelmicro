import httpx

from grelmicro.resilience import retrying


async def submit(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    async for attempt in retrying(when=httpx.HTTPError, attempts=3):
        async with attempt:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    return {}


async def main() -> dict:
    async with httpx.AsyncClient() as client:
        return await submit(client, "https://example.com", {"k": "v"})
