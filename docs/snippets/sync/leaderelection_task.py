from grelmicro.sync import LeaderElection
from grelmicro.task import Tasks

leader = LeaderElection("cluster_group")
task = Tasks()
task.add_task(leader)
