from grelmicro.resilience import GCRA, RateLimiter

invite_limiter = RateLimiter("invite", algorithm=GCRA(limit=5, window=3600))


async def is_locked(code: str) -> bool:
    result = await invite_limiter.peek(key=code)
    return not result.allowed
