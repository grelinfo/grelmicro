import asyncio

from grelmicro.coordination import ReadWriteLock
from grelmicro.providers.memory import MemoryProvider


async def main() -> None:
    # Memory keeps this demo in one process.
    # The calls are the same on every backend.
    async with MemoryProvider() as provider:
        catalog = ReadWriteLock(
            "catalog", backend=provider.readwritelock_backend()
        )

        async with catalog.read as reading:
            print("read under generation", reading.generation)

        async with catalog.write as writing:
            print("write with fencing token", writing.fencing_token)


asyncio.run(main())
