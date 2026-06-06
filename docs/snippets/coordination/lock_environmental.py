from grelmicro.coordination import Lock

# With GREL_LOCK_CART_LEASE_DURATION=60 and GREL_LOCK_CART_RETRY_INTERVAL=0.1
# present in the environment, Lock("cart") resolves both from env.
# Fields not set in env fall back to LockConfig defaults.
lock = Lock("cart")


async def main():
    async with lock:
        print("Protected resource accessed")
