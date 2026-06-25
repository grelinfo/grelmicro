import asyncio

from grelmicro.coordination import LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter

leader = LeaderElection("cluster_group", backend=MemoryLeaderElectionAdapter())


async def main():
    async with asyncio.TaskGroup() as tg:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(leader(ready=ready))
        await ready
        await asyncio.Event().wait()  # sleep forever
