import asyncio

from grelmicro.coordination import LeaderElection
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
leader = LeaderElection("cluster_group", backend=redis.leaderelection())


async def main():
    async with asyncio.TaskGroup() as tg:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(leader(ready=ready))
        await ready
        await asyncio.Event().wait()  # sleep forever
