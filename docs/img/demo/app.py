"""A rate-limited FastAPI route and a health check, on the in-memory backend."""

from fastapi import FastAPI

from grelmicro.health import HealthChecks
from grelmicro.health.fastapi import health_router
from grelmicro.resilience import MemoryRateLimiterAdapter, RateLimiter

app = FastAPI()
health = HealthChecks()
app.include_router(health_router(health))

limiter = RateLimiter.token_bucket(
    "quotes", capacity=5, refill_rate=1, backend=MemoryRateLimiterAdapter()
)


@app.get("/quote")
async def quote() -> dict[str, str]:
    """Serve a quote, rate limited to a 5-burst then 1 per second."""
    if not await limiter.allow():
        return {"status": "slow down"}
    return {"quote": "one toolkit, many patterns"}
