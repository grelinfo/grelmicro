import httpx

from grelmicro.resilience import Retry

# Reads the config from environment variables. The backoff field is
# a discriminated union; pass it as a single JSON object.
#
# - GREL_RETRY_PAYMENTS_ATTEMPTS=5
# - GREL_RETRY_PAYMENTS_ON=httpx.HTTPError
# - GREL_RETRY_PAYMENTS_BACKOFF={"type":"exponential","base_delay":0.2}
policy = Retry("payments", on=httpx.HTTPError)
