from grelmicro.resilience import RateLimiter

api_limiter = RateLimiter.token_bucket("api", capacity=100, refill_rate=10)


async def serve(user_id: str) -> None:
    # Block until a token frees up, then proceed.
    await api_limiter.wait(key=user_id)


async def serve_bounded(user_id: str) -> None:
    # Give up after 2 seconds, raising RateLimitExceededError.
    await api_limiter.wait(key=user_id, cost=3, max_wait=2.0)
