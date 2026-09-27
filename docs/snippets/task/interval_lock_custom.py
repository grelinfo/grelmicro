from grelmicro.coordination import TaskLock
from grelmicro.task import Tasks

task = Tasks()


@task.every(
    seconds=60,
    gate=TaskLock(lease_duration=600, min_hold_duration=60),
)
async def long_task():
    print("Running long task...")
