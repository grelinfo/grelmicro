import asyncio

from grelmicro.resilience import shield


async def do_work() -> None:
    return None


@shield
async def cheap_call() -> None:
    async with asyncio.timeout(5):
        await do_work()
