from logging import getLogger

from grelmicro.logging import RateLimitFilter

# One shared bucket across every record the handler sees.
# Useful on the root or app-level handler as a global safety net.
root = getLogger()
root.addFilter(RateLimitFilter(capacity=100, refill_rate=10, key_mode="global"))
