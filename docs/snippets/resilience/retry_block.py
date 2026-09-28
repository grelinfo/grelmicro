import asyncio

import httpx
from pydantic import BaseModel

from grelmicro.resilience import retrying


class Payment(BaseModel):
    amount: int


class Receipt(BaseModel):
    id: str


def payments_api(request: httpx.Request) -> httpx.Response:
    """Stand in for the payment API, so the example runs offline."""
    return httpx.Response(200, json={"id": "pay_1"})


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


async def main() -> None:
    transport = httpx.MockTransport(payments_api)
    async with httpx.AsyncClient(transport=transport) as client:
        receipt = await submit(
            client, "https://payments.example/charges", Payment(amount=100)
        )
        print(receipt)


if __name__ == "__main__":
    asyncio.run(main())
