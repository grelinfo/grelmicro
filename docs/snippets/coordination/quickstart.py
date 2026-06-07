from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(
    uses=[Coordination(election=MemoryLeaderElectionBackend()), tasks]
)

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


@tasks.interval(seconds=10, leader=leader)
async def run_once_in_the_cluster() -> None:
    print("only the leader runs this")
