import asyncio

from grelmicro.coordination import ReadWriteLock
from grelmicro.providers.memory import MemoryProvider


async def rebuild(rows: list[str]) -> list[str]:
    return sorted(rows)


async def main() -> None:
    async with MemoryProvider() as provider:
        catalog = ReadWriteLock("catalog", backend=provider.readwritelock())

        async with catalog.write as writing:
            if writing.poisoned:
                print("the previous writer died mid-write, repairing")
            rows = await rebuild(["pear", "apple"])

            reading = await writing.downgrade()
            print("still holding, now as a reader", reading.generation, rows)


asyncio.run(main())
