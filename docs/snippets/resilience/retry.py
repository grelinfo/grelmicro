import asyncio

import httpx

from grelmicro.resilience import retry


@retry(when=httpx.HTTPError, attempts=3)
async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


async def main() -> None:
    async with httpx.AsyncClient() as client:
        content = await fetch(client, "https://example.com")
        print(len(content))


if __name__ == "__main__":
    asyncio.run(main())
