import asyncio

import httpx

from grelmicro.resilience import Retry, Stack, Timeout

NAME = "recs"

recs = Stack(
    NAME,
    patterns=[
        Retry.exponential(NAME, when=httpx.HTTPError, attempts=3),
        Timeout(NAME, seconds=1.0),
    ],
)


def recs_api(request: httpx.Request) -> httpx.Response:
    """Stand in for the recommendations API, so the example runs offline."""
    return httpx.Response(200, json={"items": []})


async def main() -> None:
    transport = httpx.MockTransport(recs_api)
    async with httpx.AsyncClient(
        base_url="https://recs.example", transport=transport
    ) as client:
        response = await recs.run(client.get, "/recs/42")
        print(response.status_code)


if __name__ == "__main__":
    asyncio.run(main())
