from grelmicro.coordination import LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.task import Tasks

leader = LeaderElection("my-service", backend=MemoryLeaderElectionBackend())
task = Tasks()
task.add_task(leader)


@task.interval(seconds=5)
async def my_task():
    if leader.is_leader():
        print("Hello from the leader!")
