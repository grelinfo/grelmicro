import httpx

from grelmicro.resilience import Retry

policy = Retry.exponential(
    "payments",
    when=httpx.HTTPError,
    attempts=5,
    base_delay=0.2,
    max_delay=10.0,
    jitter="full",
)


@policy
async def call_payments():
    pass
