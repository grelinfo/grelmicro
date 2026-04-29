from grelmicro.resilience import RateLimiter

auth_limiter = RateLimiter.gcra("auth", limit=5, window=60)


def verify_password(password: str) -> bool:
    return True


async def login(ip: str, password: str) -> None:
    await auth_limiter.acquire_or_raise(key=ip)

    if verify_password(password):
        # Successful login: clear the failure counter
        await auth_limiter.reset(key=ip)
