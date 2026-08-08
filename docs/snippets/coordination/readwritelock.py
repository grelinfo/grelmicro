import asyncio

from grelmicro.coordination import ReadWriteLock
from grelmicro.providers.memory import MemoryProvider


async def main() -> None:
    async with MemoryProvider() as provider:
        catalog = ReadWriteLock("catalog", backend=provider.readwritelock())

        async with catalog.read as reading:
            print("read under generation", reading.generation)

        async with catalog.write as writing:
            print("write with fencing token", writing.fencing_token)


asyncio.run(main())
