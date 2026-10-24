from grelmicro import Grelmicro
from grelmicro.coordination import LeaderElection
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

leader = LeaderElection("primary")
tasks = Tasks()
tasks.add_task(leader)


@tasks.cron("0 3 * * *", gate=leader, name="nightly-report")
async def nightly_report():
    print("Building the report")


micro = Grelmicro(uses=[MemoryProvider(), tasks])


def test_nightly_report_runs_on_the_leader():
    [task] = [t for t in micro.describe().tasks if t.name == "nightly-report"]
    assert task.cron == "0 3 * * *"
    assert task.gate == "leader"
    assert task.leader == "primary"
    assert task.claim_name == "nightly-report"
