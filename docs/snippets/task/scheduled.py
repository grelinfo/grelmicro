from grelmicro.task import TaskManager

task = TaskManager()


@task.scheduled(seconds=60)
async def cleanup():
    print("Running cleanup...")
