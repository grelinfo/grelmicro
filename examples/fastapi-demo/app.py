"""A runnable FastAPI app exercising every grelmicro Pattern.

Every endpoint is the bare minimum to show one Pattern, with a one-line
comment naming it. Run it with `docker compose up --wait` (see README).
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from grelmicro import Grelmicro
from grelmicro.cache import Cache, TTLCache
from grelmicro.cache.cached import cached
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.coordination import Coordination, LeaderElection, Lock
from grelmicro.fastapi import GrelmicroMiddleware
from grelmicro.health import HealthChecks
from grelmicro.health.fastapi import health_router
from grelmicro.log import configure
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    RateLimiter,
    RateLimiterRegistry,
)
from grelmicro.resilience.errors import CircuitBreakerError
from grelmicro.task import Tasks

logger = logging.getLogger("demo")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
POSTGRES_URL = os.environ.get(
    "POSTGRES_URL", "postgresql://demo:demo@localhost:5432/demo"
)

# === grelmicro wiring ===
# One Redis for cache / rate limiting / locks, one Postgres for the
# fleet-wide circuit breaker. Both providers are lifecycled by the app.
redis = RedisProvider(REDIS_URL)
postgres = PostgresProvider(POSTGRES_URL)

tasks = Tasks()
leader = LeaderElection("demo-leader")
tasks.add_task(leader)

# auto_health registers a provider:{short_name} readiness check for every
# active provider, so /readyz probes Redis and Postgres with no boilerplate.
health = HealthChecks(auto_health=True)

micro = Grelmicro(
    uses=[
        Cache(redis.cache()),
        RateLimiterRegistry(redis.ratelimiter()),
        CircuitBreakerRegistry(postgres.circuitbreaker()),
        Coordination(lock=redis.lock(), election=redis.leaderelection()),
        health,
        tasks,
    ]
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure()  # structured JSON logging
    async with micro:
        logger.info("demo started")
        yield
        logger.info("demo stopped")


app = FastAPI(title="grelmicro demo", lifespan=lifespan)
# The middleware binds the app inside request handlers, so every Pattern
# below resolves its backend ambiently, same as the background tasks.
app.add_middleware(GrelmicroMiddleware, micro=micro)
app.include_router(health_router(health))  # GET /livez, /readyz, /healthz


# --- Cache: @cached over the Cache backend, with stampede protection ---
catalog = TTLCache(ttl=30, serializer=JsonSerializer())


@app.get("/product/{product_id}")
@cached(catalog, lock=True)
async def get_product(product_id: int) -> dict:
    # Cache Pattern: the second call within the TTL skips this body.
    return {"id": product_id, "name": f"Product {product_id}"}


# --- Rate limiter: token bucket per client ---
api_limiter = RateLimiter.token_bucket("api", capacity=5, refill_rate=1)


@app.get("/quote")
async def quote(client: str = "anon") -> dict:
    # Rate-limiter Pattern: 5 burst, then 1 per second per client.
    result = await api_limiter.acquire(key=client)
    if not result.allowed:
        raise HTTPException(status_code=429, detail="slow down")
    return {"quote": "the cost of a thing is the life you exchange for it"}


# --- Circuit breaker: trips after repeated failures to a flaky service ---
breaker = CircuitBreaker("flaky-service")


@app.get("/flaky")
async def flaky(fail: bool = False) -> dict:
    # Circuit-breaker Pattern: opens after the failure threshold.
    try:
        async with breaker:
            if fail:
                msg = "upstream failed"
                raise RuntimeError(msg)
            return {"status": "ok"}
    except CircuitBreakerError as exc:
        raise HTTPException(status_code=503, detail="circuit open") from exc


# --- Distributed lock: serialize a ledger update across replicas ---
ledger_lock = Lock("ledger")


@app.post("/ledger")
async def update_ledger(amount: int) -> dict:
    # Distributed-lock Pattern: only one replica updates at a time.
    async with ledger_lock:
        return {"applied": amount}


# --- Leader-gated task: only the elected leader runs the sweep ---
@tasks.interval(seconds=10, leader=leader)
def nightly_sweep() -> None:
    # Leader-election Pattern: runs on exactly one replica.
    logger.info("nightly sweep (leader only)")


# --- Local interval task: runs on every replica ---
@tasks.interval(seconds=5)
def heartbeat() -> None:
    logger.info("heartbeat")
