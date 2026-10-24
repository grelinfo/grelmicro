from grelmicro import Grelmicro
from grelmicro.coordination import LeaderElection
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import CronTask, Tasks

leader = LeaderElection("primary")
tasks = Tasks()
tasks.add_task(leader)


@tasks.cron("0 3 * * *", gate=leader, name="nightly-report")
async def nightly_report():
    print("Building the report")


micro = Grelmicro(uses=[MemoryProvider(), tasks])


def test_scheduler_is_wired():
    report = micro.describe()
    assert report.tasks is not None
    assert "nightly-report" in {task.name for task in report.tasks}


def test_nightly_report_runs_on_the_leader():
    [task] = [t for t in tasks.tasks if t.name == "nightly-report"]
    assert isinstance(task, CronTask)
    assert task.gate is leader
