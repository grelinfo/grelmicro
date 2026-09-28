import asyncio

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.providers.memory import MemoryProvider

# Memory keeps this example in one process.
# The calls are the same on every backend.
micro = Grelmicro(uses=[Coordination(MemoryProvider(), requires="process")])

# With GREL_LOCK_CART_LEASE_DURATION=60 and GREL_LOCK_CART_RETRY_INTERVAL=0.1
# present in the environment, Lock("cart") resolves both from env.
# Fields not set in env fall back to LockConfig defaults.
lock = Lock("cart")


async def main() -> None:
    async with micro, lock:
        print("Protected resource accessed")


if __name__ == "__main__":
    asyncio.run(main())
