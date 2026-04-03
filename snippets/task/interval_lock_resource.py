from grelmicro.sync import Lock
from grelmicro.task import TaskManager

task = TaskManager()
resource_lock = Lock("shared-resource")


@task.interval(seconds=60, max_lock_seconds=300, sync=resource_lock)
async def cleanup():
    print("Running cleanup...")
