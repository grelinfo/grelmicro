from grelmicro.coordination import Lock
from grelmicro.task import Tasks

task = Tasks()
resource_lock = Lock("shared-resource")


@task.interval(seconds=60, lease_duration=300, sync=resource_lock)
async def cleanup():
    print("Running cleanup...")
