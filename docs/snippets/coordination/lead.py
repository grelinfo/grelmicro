import asyncio

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

tasks = Tasks()
redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, tasks])

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


async def emit_metrics() -> None:
    while True:  # cancelled the instant leadership is lost
        print("leader heartbeat")
        await asyncio.sleep(10)


async def run() -> None:
    # Runs only while leader, re-running after any re-acquisition.
    await leader.lead(emit_metrics, repeat=True)
