import asyncio

from grelmicro import Grelmicro
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(uses=[tasks])


@tasks.every(seconds=5)
async def cleanup() -> None:
    print("cleanup")


async def main() -> None:
    # The app runs the schedule until the block is left.
    async with micro:
        await asyncio.sleep(12)


if __name__ == "__main__":
    asyncio.run(main())
