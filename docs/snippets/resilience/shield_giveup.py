import asyncio

import httpx

from grelmicro.resilience import shield


@shield.api(timeout_errors=(httpx.TimeoutException,))
async def fetch(url: str) -> bytes:
    raise httpx.TimeoutException("dependency stalled")


async def main() -> None:
    try:
        await fetch("https://example.com/recs")
    except httpx.TimeoutException as exc:
        print(exc.__notes__)
        # ['shield: budget exhausted after 4/4 attempts in 18.30s (api profile)']


if __name__ == "__main__":
    asyncio.run(main())
