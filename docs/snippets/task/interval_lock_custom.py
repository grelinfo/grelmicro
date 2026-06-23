from grelmicro.coordination import TaskLock
from grelmicro.task import Tasks

task = Tasks()


@task.interval(
    seconds=60,
    lock=TaskLock(lease_duration=300, min_hold_duration=30),
)
async def long_task():
    print("Running long task...")
