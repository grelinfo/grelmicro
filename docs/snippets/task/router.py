from grelmicro.task import TaskRouter


router = TaskRouter()


@router.every(interval=5)
async def my_task():
    print("Hello, World!")


from grelmicro.task import Tasks

tasks = Tasks()
tasks.include_router(router)
