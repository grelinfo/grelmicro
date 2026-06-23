from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter
from grelmicro.task import Tasks

leader = LeaderElection("cluster_group", backend=MemoryLeaderElectionAdapter())
coordination = Coordination(election=leader.backend)
task = Tasks()
task.add_task(leader)
