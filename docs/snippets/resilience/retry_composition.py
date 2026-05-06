import httpx

from grelmicro.resilience import CircuitBreaker, retry

cb = CircuitBreaker("payments")


# A narrow allowlist that excludes CircuitBreakerError. When the
# breaker is open it raises CircuitBreakerError, which is not in
# `on`, so the retry loop aborts immediately.
@retry(on=(httpx.ConnectError, httpx.TimeoutException), attempts=3)
async def call_payments(url: str, payload: dict) -> dict:
    async with cb:
        response = await httpx.AsyncClient().post(url, json=payload)
        response.raise_for_status()
        return response.json()
