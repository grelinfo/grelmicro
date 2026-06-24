from grelmicro.coordination import TaskLock
from grelmicro.task import Tasks

task = Tasks()


@task.every(seconds=60, lock=TaskLock(lease_duration=300))
async def cleanup():
    print("Running cleanup...")
