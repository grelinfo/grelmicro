from grelmicro.task import TaskRouter


router = TaskRouter()


@router.interval(seconds=5)
async def my_task():
    print("Hello, World!")


from grelmicro.task.manager import TaskManager

task = TaskManager()
task.include_router(router)
