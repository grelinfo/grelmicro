import asyncio

from grelmicro.coordination import ReadGuard, ReadWriteLock, WriteGuard
from grelmicro.providers.memory import MemoryProvider


class Catalog:
    """A resource that only accepts writes carrying a higher fencing token."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.highest_token = 0

    def read_all(self, guard: ReadGuard) -> list[str]:
        print("reading catalog", guard.name)
        return list(self.rows)

    def replace_all(self, guard: WriteGuard, rows: list[str]) -> bool:
        if guard.fencing_token <= self.highest_token:
            return False
        self.highest_token = guard.fencing_token
        self.rows = rows
        return True


async def main() -> None:
    catalog = Catalog()

    # Memory keeps this demo in one process. Every backend behaves the same.
    async with MemoryProvider() as provider:
        lock = ReadWriteLock("catalog", backend=provider.readwritelock())

        async with lock.write as writing:
            assert catalog.replace_all(writing, ["apple", "pear"])

        async with lock.read as reading:
            assert catalog.read_all(reading) == ["apple", "pear"]


asyncio.run(main())
