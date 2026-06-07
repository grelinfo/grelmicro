from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.task import Tasks

leader = LeaderElection("cluster_group", backend=MemoryLeaderElectionBackend())
coordination = Coordination(election=leader.backend)
task = Tasks()
task.add_task(leader)
