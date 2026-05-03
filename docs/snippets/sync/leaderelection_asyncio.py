import asyncio

from grelmicro.sync import LeaderElection

leader = LeaderElection("cluster_group")


async def main():
    async with asyncio.TaskGroup() as tg:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(leader(ready=ready))
        await ready
        await asyncio.Event().wait()  # sleep forever
