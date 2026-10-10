from grelmicro.coordination import Lock
from grelmicro.task import Tasks

tasks = Tasks()
resource_lock = Lock("shared-resource")


@tasks.every(interval=60, gate="claim", sync=resource_lock)
async def cleanup():
    print("Running cleanup...")
