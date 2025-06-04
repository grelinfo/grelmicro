from grelmicro.sync import LeaderElection
from grelmicro.task import TaskManager

leader = LeaderElection("cluster_group")
task = TaskManager()
task.add_task(leader)
