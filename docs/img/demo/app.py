"""Recording app for the README demo GIF.

A single file, no infrastructure. It shows the grelmicro pitch in one
screen: a FastAPI route protected by a rate limiter and a health check,
both running on the in-memory backend so the demo needs no Redis.
"""

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
