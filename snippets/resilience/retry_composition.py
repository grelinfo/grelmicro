import httpx

from grelmicro.resilience import CircuitBreaker, retry

cb = CircuitBreaker("payments")


# A narrow allowlist that excludes CircuitBreakerError. When the
# breaker is open it raises CircuitBreakerError, which is not in
# `on`, so the retry loop aborts immediately.
@retry(when=(httpx.ConnectError, httpx.TimeoutException), attempts=3)
async def call_payments(
    client: httpx.AsyncClient, url: str, payload: dict
) -> dict:
    async with cb:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def main() -> dict:
    async with httpx.AsyncClient() as client:
        return await call_payments(client, "https://example.com", {"k": "v"})
