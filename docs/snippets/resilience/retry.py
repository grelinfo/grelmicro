import asyncio

import httpx

from grelmicro.resilience import retry


def api(request: httpx.Request) -> httpx.Response:
    """Stand in for a remote API, so the example runs offline."""
    return httpx.Response(200, content=b"hello")


@retry(when=httpx.HTTPError, attempts=3)
async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


async def main() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        content = await fetch(client, "https://api.example/greeting")
        print(content)


if __name__ == "__main__":
    asyncio.run(main())
