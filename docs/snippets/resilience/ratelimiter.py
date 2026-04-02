from grelmicro.resilience.ratelimiter import RateLimiter

# Create rate limiters for different concerns
auth_limiter = RateLimiter("auth", limit=5, window=60)
api_limiter = RateLimiter("api", limit=100, window=60)


async def login(ip: str) -> None:
    result = await auth_limiter.acquire(key=ip)
    if not result.allowed:
        print(f"Too many attempts, retry after {result.retry_after:.0f}s")
        return
    print(f"Login allowed, {result.remaining} attempts remaining")


async def api_call(user_id: str) -> None:
    # Raises RateLimitExceededError if limit exceeded
    await api_limiter.acquire_or_raise(key=user_id)
    print("API call allowed")
