from grelmicro.coordination import Lock, TaskLock
from grelmicro.task import Tasks

task = Tasks()
resource_lock = Lock("shared-resource")


@task.interval(
    seconds=60, lock=TaskLock(lease_duration=300), sync=resource_lock
)
async def cleanup():
    print("Running cleanup...")
