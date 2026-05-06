import httpx

from grelmicro.resilience import retrying


async def submit(url: str, payload: dict) -> dict:
    async for attempt in retrying(on=httpx.HTTPError, attempts=3):
        async with attempt:
            response = await httpx.AsyncClient().post(url, json=payload)
            response.raise_for_status()
            return response.json()
    return {}
