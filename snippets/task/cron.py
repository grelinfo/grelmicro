from grelmicro.task import Tasks

task = Tasks()


@task.cron("0 2 * * *", timezone="Europe/Zurich")
async def nightly_report():
    print("Running the nightly report at 02:00 Zurich time")
