from grelmicro.resilience import MemoryTokenBucket

# Sync, thread-safe, zero-I/O primitive.
# Useful for CLI tools, shell helpers, and other sync hot paths
# where the async RateLimiter isn't appropriate.
bucket = MemoryTokenBucket(capacity=5, refill_rate=1)


def handle_event(event_id: str) -> None:
    if not bucket.try_acquire(key=event_id):
        return
    # ... process the event
