import asyncio

import httpx
from pydantic import BaseModel

from grelmicro.resilience import (
    CircuitBreaker,
    MemoryCircuitBreakerAdapter,
    retry,
)

cb = CircuitBreaker("payments", backend=MemoryCircuitBreakerAdapter())


class Payment(BaseModel):
    amount: int


class Receipt(BaseModel):
    id: str


def payments_api(request: httpx.Request) -> httpx.Response:
    """Stand in for the payment API, so the example runs offline."""
    return httpx.Response(200, json={"id": "pay_1"})


# A narrow allowlist that excludes CircuitBreakerError. When the
# breaker is open it raises CircuitBreakerError, which is not in
# `when`, so the retry loop aborts immediately.
@retry(when=(httpx.ConnectError, httpx.TimeoutException), attempts=3)
async def call_payments(
    client: httpx.AsyncClient, url: str, payment: Payment
) -> Receipt:
    async with cb:
        response = await client.post(url, json=payment.model_dump())
        response.raise_for_status()
        return Receipt.model_validate(response.json())


async def main() -> None:
    transport = httpx.MockTransport(payments_api)
    async with httpx.AsyncClient(transport=transport) as client:
        receipt = await call_payments(
            client, "https://payments.example/charges", Payment(amount=100)
        )
        print(receipt)


if __name__ == "__main__":
    asyncio.run(main())
