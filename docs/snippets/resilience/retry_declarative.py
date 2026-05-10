import httpx

from grelmicro.resilience import (
    ExponentialBackoff,
    Retry,
    RetryConfig,
)

config = RetryConfig(
    attempts=5,
    on=(httpx.HTTPError,),
    backoff=ExponentialBackoff(base_delay=0.2, max_delay=10.0, jitter="full"),
)
policy = Retry("payments", config=config)
