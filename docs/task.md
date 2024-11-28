# Task Scheduler

The `task` package provides a simple task scheduler that can be used to run tasks periodically.

> **Note**: This is not a replacement for bigger tools like Celery, taskiq, or APScheduler. It is just lightweight, easy to use, and safe for running tasks in a distributed system with synchronization.

The key features are:

- **Fast & Easy**: Offers simple decorators to define and schedule tasks effortlessly.
- **Interval Task**: Allows tasks to run at specified intervals.
- **Synchronization**: Controls concurrency using synchronization primitives to manage simultaneous task execution (see the `sync` package).
- **Dependency Injection**: Use [FastDepends](https://lancetnik.github.io/FastDepends/) library to inject dependencies into tasks.
- **Error Handling**: Catches and logs errors, ensuring that task execution failures do not stop the scheduling.

## Task Manager

The `TaskManager` class is the main entry point to manage scheduled tasks. You need to start the task manager to run the scheduled tasks using the application lifespan.

=== "FastAPI"

    ```python
    {!> ../examples/task/fastapi.py!}
    ```

=== "FastStream"

    ```python

    {!> ../examples/task/faststream.py!}
    ```

## Interval Task

To create an `IntervalTask`, use the `interval` decorator method of the `TaskManager` instance. This decorator allows tasks to run at specified intervals.

> **Note**: The interval specifies the waiting time between task executions. Ensure that the task execution duration is considered to meet deadlines effectively.

=== "TaskManager"

    ```python
    {!> ../examples/task/interval_manager.py!}
    ```

=== "TaskRouter"

    ```python
    {!> ../examples/task/interval_router.py!}
    ```


## Synchronization

The Task can be synchronized using a [Synchoronization Primitive](sync.md) to control concurrency and manage simultaneous task execution.

=== "Lock"

    ```python
    {!> ../examples/task/lock.py!}
    ```


=== "Leader Election"


    ```python
    {!> ../examples/task/leaderelection.py!}
    ```

## Task Router

For bigger applications, you can use the `TaskRouter` class to manage tasks in different modules.


```python
{!> ../examples/task/router.py [ln:1-10]!}
```

Then you can include the `TaskRouter` into the `TaskManager` or other routers using the `include_router` method.

```python
{!> ../examples/task/router.py [ln:12-]!}
```

!!! tip
    The `TaskRouter` follows the same philosophy as the `APIRouter` in FastAPI or the **Router** in FastStream.
