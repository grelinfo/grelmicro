from grelmicro.task import Tasks

tasks = Tasks(timezone="Europe/Zurich")


@tasks.cron("0 2 * * *")
async def nightly_report():
    print("Runs at 02:00 Zurich time, the timezone configured above")


@tasks.cron("0 0 1 * *", timezone="UTC")
async def monthly_rollup():
    print("Runs at midnight UTC, whatever the service is configured with")
