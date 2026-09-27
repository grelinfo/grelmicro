from grelmicro.coordination import Lock
from grelmicro.task import Tasks

task = Tasks()
resource_lock = Lock("shared-resource")


@task.every(seconds=60, gate="claim", sync=resource_lock)
async def cleanup():
    print("Running cleanup...")
