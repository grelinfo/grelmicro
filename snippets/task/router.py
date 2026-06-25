from grelmicro.task import TaskRouter


router = TaskRouter()


@router.every(seconds=5)
async def my_task():
    print("Hello, World!")


from grelmicro.task import Tasks

task = Tasks()
task.include_router(router)
