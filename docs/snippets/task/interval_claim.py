from grelmicro.task import Tasks

tasks = Tasks()


@tasks.every(seconds=60, gate="claim")
async def cleanup():
    print("Running cleanup...")
