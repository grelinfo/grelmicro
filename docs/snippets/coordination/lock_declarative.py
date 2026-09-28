import asyncio

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.lock import LockConfig
from grelmicro.providers.memory import MemoryProvider

# Memory keeps this example in one process.
# The calls are the same on every backend.
micro = Grelmicro(uses=[Coordination(MemoryProvider(), requires="process")])

config = LockConfig(
    worker="web-1",
    lease_duration=60,
    retry_interval=0.1,
)
lock = Lock.from_config("cart", config)


async def main() -> None:
    async with micro, lock:
        print("Protected resource accessed")


if __name__ == "__main__":
    asyncio.run(main())
