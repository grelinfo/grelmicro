import time

from grelmicro.health import HealthChecks, HealthDetails, HealthError, Liveness

MAX_IDLE = 60
"""Seconds the consumer may go without handling a message."""

health = HealthChecks(liveness=Liveness(stall_timeout=30, failure_threshold=3))
last_handled = time.monotonic()


@health.check("consumer-progress", liveness=True)
async def check_consumer_progress() -> HealthDetails | None:
    if time.monotonic() - last_handled > MAX_IDLE:
        msg = f"no message handled in {MAX_IDLE} s"
        raise HealthError(msg)
    return None
