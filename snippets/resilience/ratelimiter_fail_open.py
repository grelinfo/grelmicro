from grelmicro.resilience.ratelimiter import RateLimiter

# Non-critical limiter: prefer availability over strictness
limiter = RateLimiter("analytics", limit=100, window=60, fail_open=True)


def record_event(user_id: str) -> None: ...


async def track_event(user_id: str) -> None:
    # If Redis is down, the event is still tracked
    result = await limiter.acquire(key=user_id)
    if result.allowed:
        record_event(user_id)
