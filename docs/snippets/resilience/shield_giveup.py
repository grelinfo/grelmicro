import httpx

from grelmicro.resilience import shield


@shield.api(timeout_errors=(httpx.TimeoutException,))
async def fetch(url: str) -> bytes:
    raise httpx.TimeoutException("dependency stalled")


async def main(url: str) -> None:
    try:
        await fetch(url)
    except httpx.TimeoutException as exc:
        print(exc.__notes__)
        # ['shield: budget exhausted after 4/4 attempts in 18.30s (api profile)']
