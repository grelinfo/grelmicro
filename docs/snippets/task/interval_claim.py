from grelmicro.task import Tasks

task = Tasks()


@task.every(seconds=60, gate="claim")
async def cleanup():
    print("Running cleanup...")
