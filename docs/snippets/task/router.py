from grelmicro.task import TaskRouter


router = TaskRouter()


@router.interval(seconds=5)
async def my_task():
    print("Hello, World!")


from grelmicro.task import Tasks

task = Tasks()
task.include_router(router)
