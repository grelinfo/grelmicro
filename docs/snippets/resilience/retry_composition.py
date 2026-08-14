import httpx
from pydantic import BaseModel

from grelmicro.resilience import CircuitBreaker, retry

cb = CircuitBreaker("payments")


class Payment(BaseModel):
    amount: int


class Receipt(BaseModel):
    id: str


# A narrow allowlist that excludes CircuitBreakerError. When the
# breaker is open it raises CircuitBreakerError, which is not in
# `on`, so the retry loop aborts immediately.
@retry(when=(httpx.ConnectError, httpx.TimeoutException), attempts=3)
async def call_payments(
    client: httpx.AsyncClient, url: str, payment: Payment
) -> Receipt:
    async with cb:
        response = await client.post(url, json=payment.model_dump())
        response.raise_for_status()
        return Receipt.model_validate(response.json())


async def main() -> Receipt:
    async with httpx.AsyncClient() as client:
        return await call_payments(
            client, "https://example.com", Payment(amount=100)
        )
