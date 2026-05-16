from grelmicro.resilience import RateLimiter

invite_limiter = RateLimiter.sliding_window("invite", limit=5, window=3600)


async def is_locked(code: str) -> bool:
    result = await invite_limiter.peek(key=code)
    return not result.allowed
