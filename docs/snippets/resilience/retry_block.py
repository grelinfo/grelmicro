import httpx
from pydantic import BaseModel

from grelmicro.resilience import retrying


class Payment(BaseModel):
    amount: int


class Receipt(BaseModel):
    id: str


async def submit(
    client: httpx.AsyncClient, url: str, payment: Payment
) -> Receipt:
    async for attempt in retrying(when=httpx.HTTPError, attempts=3):
        async with attempt:
            response = await client.post(url, json=payment.model_dump())
            response.raise_for_status()
            return Receipt.model_validate(response.json())
    msg = "retrying returns or raises, never falls through"
    raise AssertionError(msg)


async def main() -> Receipt:
    async with httpx.AsyncClient() as client:
        return await submit(client, "https://example.com", Payment(amount=100))
