import asyncio

from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(uses=[MemoryProvider(), tasks])

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


async def emit_metrics() -> None:
    while True:  # cancelled the instant leadership is lost
        print("leader heartbeat")
        await asyncio.sleep(10)


async def run() -> None:
    # Runs only while leader, re-running after any re-acquisition.
    await leader.lead(emit_metrics, repeat=True)
