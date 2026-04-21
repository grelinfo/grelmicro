from logging import getLogger

from grelmicro.logging import RateLimitFilter

# Allow a burst of 10 records per logger, then 1 record/sec sustained.
logger = getLogger("grelmicro.ingest")
logger.addFilter(RateLimitFilter(capacity=10, refill_rate=1))
