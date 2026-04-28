from grelmicro.resilience import GCRAConfig, RateLimiter

invite_limiter = RateLimiter("invite", GCRAConfig(limit=5, window=3600))


async def is_locked(code: str) -> bool:
    result = await invite_limiter.peek(key=code)
    return not result.allowed
