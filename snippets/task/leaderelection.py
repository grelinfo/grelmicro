from grelmicro.sync import LeaderElection
from grelmicro.task import TaskManager

leader = LeaderElection("my-service")
task = TaskManager()
task.add_task(leader)


@task.interval(seconds=5)
async def my_task():
    if leader.is_leader():
        print("Hello from the leader!")
