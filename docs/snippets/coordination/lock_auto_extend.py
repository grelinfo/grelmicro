import asyncio
from datetime import timedelta

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.providers.memory import MemoryProvider

# Memory keeps this example in one process.
# The calls are the same on every backend.
micro = Grelmicro(uses=[Coordination(MemoryProvider(), requires="process")])

lock = Lock("migration", lease_duration=timedelta(seconds=1))


async def migrate(*, fencing_token: int) -> None:
    # Runs for two leases. The lease is extended every third of it.
    await asyncio.sleep(2)
    print(f"Migrated with fencing token {fencing_token}")


async def main() -> None:
    async with micro, lock.hold(timeout=5, auto_extend=True) as held:
        await migrate(fencing_token=held.fencing_token)


if __name__ == "__main__":
    asyncio.run(main())
