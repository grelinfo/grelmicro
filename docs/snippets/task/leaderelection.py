from grelmicro.coordination import LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter
from grelmicro.task import Tasks

leader = LeaderElection("my-service", backend=MemoryLeaderElectionAdapter())
task = Tasks()
task.add_task(leader)


@task.every(seconds=5)
async def my_task():
    if leader.is_leader():
        print("Hello from the leader!")
