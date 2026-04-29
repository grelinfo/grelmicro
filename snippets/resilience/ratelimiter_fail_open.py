from grelmicro.resilience import RateLimiter

# Non-critical limiter: prefer availability over strictness
limiter = RateLimiter.token_bucket(
    "analytics",
    capacity=100,
    refill_rate=10,
    fail_open=True,
)


def record_event(user_id: str) -> None: ...


async def track_event(user_id: str) -> None:
    # If Redis is down, the event is still tracked
    result = await limiter.acquire(key=user_id)
    if result.allowed:
        record_event(user_id)
