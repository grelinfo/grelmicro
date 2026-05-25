"""FastStream consumer protected by a distributed lock and rate limiter.

The same primitives used in FastAPI routes drop into FastStream message
handlers without changes. The `Grelmicro(uses=[...])` container opens
every backend and component for the consumer's lifespan.
"""

from contextlib import asynccontextmanager

from faststream import ContextRepo, FastStream
from faststream.redis import RedisBroker

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    RateLimiter,
    RateLimiters,
    RateLimitExceededError,
)
from grelmicro.sync import Lock, Sync

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, Sync(redis), RateLimiters(redis)])

per_user_limiter = RateLimiter.sliding_window("messages", limit=10, window=60)


@asynccontextmanager
async def lifespan(context: ContextRepo):
    async with micro:
        yield


broker = RedisBroker()
app = FastStream(broker, lifespan=lifespan)


@broker.subscriber("user-events")
async def handle_user_event(message: dict) -> None:
    """Process a user event under a per-user lock and a fleet rate limit."""
    user_id = message["user_id"]

    try:
        await per_user_limiter.acquire_or_raise(key=str(user_id))
    except RateLimitExceededError:
        # Drop or requeue: enforcement is fleet-wide, so every consumer
        # replica sees the same budget per user.
        return

    async with Lock(f"user:{user_id}"):
        # Only one consumer (across the whole fleet) processes events
        # for this user at a time.
        await _process(message)


async def _process(message: dict) -> None:
    print("processing:", message)
