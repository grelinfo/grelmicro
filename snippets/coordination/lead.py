import asyncio

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(
    uses=[Coordination(election=MemoryLeaderElectionAdapter()), tasks]
)

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


async def emit_metrics() -> None:
    while True:  # cancelled the instant leadership is lost
        print("leader heartbeat")
        await asyncio.sleep(10)


async def run() -> None:
    # Runs only while leader, re-running after any re-acquisition.
    await leader.lead(emit_metrics, repeat=True)
