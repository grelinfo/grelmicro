from grelmicro.resilience import GCRA, RateLimiter, TokenBucket

# GCRA for precise sliding-window API throttling.
auth_limiter = RateLimiter("auth", algorithm=GCRA(limit=5, window=60))

# TokenBucket for burst-friendly "N then 1/sec" semantics.
api_limiter = RateLimiter(
    "api", algorithm=TokenBucket(capacity=100, refill_rate=10)
)


async def login(ip: str) -> None:
    result = await auth_limiter.acquire(key=ip)
    if not result.allowed:
        print(f"Too many attempts, retry after {result.retry_after:.0f}s")
        return
    print(f"Login allowed, {result.remaining} attempts remaining")


async def api_call(user_id: str) -> None:
    # Raises RateLimitExceededError if the bucket is empty
    await api_limiter.acquire_or_raise(key=user_id)
    print("API call allowed")
