from grelmicro.resilience import RateLimiter

invite_limiter = RateLimiter.gcra("invite", limit=5, window=3600)


async def is_locked(code: str) -> bool:
    result = await invite_limiter.peek(key=code)
    return not result.allowed
