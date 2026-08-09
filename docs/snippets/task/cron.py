from grelmicro.task import Tasks

task = Tasks(timezone="Europe/Zurich")


@task.cron("0 2 * * *")
async def nightly_report():
    print("Running the nightly report at 02:00 Zurich time")
