import httpx

from grelmicro.resilience import Retry

# Reads the rest of the config from environment variables:
# - GREL_RETRY_PAYMENTS_ATTEMPTS=5
# - GREL_RETRY_PAYMENTS_BACKOFF=exponential
# - GREL_RETRY_PAYMENTS_BASE_DELAY=0.2
# - GREL_RETRY_PAYMENTS_MAX_DELAY=10
# - GREL_RETRY_PAYMENTS_JITTER=full
policy = Retry("payments", on=httpx.HTTPError)
