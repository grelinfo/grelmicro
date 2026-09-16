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


async def main() -> None:
    async with httpx.AsyncClient(base_url="https://example.com") as client:
        response = await recs.run(client.get, "/recs/42")
        print(response.status_code)


if __name__ == "__main__":
    asyncio.run(main())
