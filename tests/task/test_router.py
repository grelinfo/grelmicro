"""Test Task Router."""

from functools import partial

import pytest

from grelmicro.backends.memory.lock import MemoryLockBackend
from grelmicro.sync.lock import LeasedLock
from grelmicro.task import TaskRouter
from grelmicro.task._interval import IntervalTask
from grelmicro.task.errors import FunctionNotSupportedError, TaskAlreadyStartedError
from tests.task.samples import EventTask, SimpleClass, test1, test2, test3


def test_router_init() -> None:
    """Test Task Router Initialization."""
    # Arrange
    custom_task = EventTask()

    # Act
    router = TaskRouter()
    router_with_task = TaskRouter(tasks=[custom_task])

    # Assert
    assert router.tasks == []
    assert router_with_task.tasks == [custom_task]


def test_router_add_task() -> None:
    """Test Task Router Add Task."""
    # Arrange
    custom_task1 = EventTask()
    custom_task2 = EventTask()
    router = TaskRouter()
    router_with_task = TaskRouter(tasks=[custom_task1])

    # Act
    router.add_task(custom_task1)
    router_with_task.add_task(custom_task2)

    # Assert
    assert router.tasks == [custom_task1]
    assert router_with_task.tasks == [custom_task1, custom_task2]


def test_router_include_router() -> None:
    """Test Task Router Include Router."""
    # Arrange
    custom_task1 = EventTask()
    custom_task2 = EventTask()
    router = TaskRouter(tasks=[custom_task1])
    router_with_task = TaskRouter(tasks=[custom_task2])

    # Act
    router.include_router(router_with_task)

    # Assert
    assert router.tasks == [custom_task1, custom_task2]


def test_router_interval() -> None:
    """Test Task Router add interval task."""
    # Arrange
    task_count = 4
    custom_task = EventTask()
    router = TaskRouter(tasks=[custom_task])
    sync = LeasedLock(backend=MemoryLockBackend(), name="testlock")

    # Act
    router.interval(name="test1", interval=10, sync=sync)(test1)
    router.interval(name="test2", interval=20)(test2)
    router.interval(10)(test3)

    # Assert
    assert len(router.tasks) == task_count
    assert (
        sum(isinstance(task, IntervalTask) for task in router.tasks) == task_count - 1
    )
    assert router.tasks[0].name == "event_task"
    assert router.tasks[1].name == "test1"
    assert router.tasks[2].name == "test2"
    assert router.tasks[3].name == "tests.task.samples:test3"


def test_router_interval_name_generation() -> None:
    """Test Task Router Interval Name Generation."""
    # Arrange
    router = TaskRouter()

    # Act
    router.interval(10)(test1)
    router.interval(10)(SimpleClass.static_method)
    router.interval(10)(SimpleClass.method)

    # Assert
    assert router.tasks[0].name == "tests.task.samples:test1"
    assert router.tasks[1].name == "tests.task.samples:SimpleClass.static_method"
    assert router.tasks[2].name == "tests.task.samples:SimpleClass.method"


def test_router_interval_name_generation_error() -> None:
    """Test Task Router Interval Name Generation Error."""
    # Arrange
    router = TaskRouter()
    test_instance = SimpleClass()

    # Act
    with pytest.raises(FunctionNotSupportedError, match="nested function"):

        @router.interval(interval=10)
        def nested_function() -> None:
            pass

    with pytest.raises(FunctionNotSupportedError, match="lambda"):
        router.interval(interval=10)(lambda _: None)

    with pytest.raises(FunctionNotSupportedError, match="method"):
        router.interval(interval=10)(test_instance.method)

    with pytest.raises(FunctionNotSupportedError, match="partial()"):
        router.interval(interval=10)(partial(test1))

    with pytest.raises(
        FunctionNotSupportedError,
        match="callable without __module__ or __qualname__ attribute",
    ):
        router.interval(interval=10)(object())  # type: ignore[arg-type]


def test_router_add_task_when_started() -> None:
    """Test Task Router Add Task When Started."""
    # Arrange
    custom_task = EventTask()
    router = TaskRouter()
    router.do_mark_as_started()

    # Act
    with pytest.raises(TaskAlreadyStartedError):
        router.add_task(custom_task)


def test_router_include_router_when_started() -> None:
    """Test Task Router Include Router When Started."""
    # Arrange
    router = TaskRouter()
    router.do_mark_as_started()
    router_child = TaskRouter()

    # Act
    with pytest.raises(TaskAlreadyStartedError):
        router.include_router(router_child)


def test_router_started_propagation() -> None:
    """Test Task Router Started Propagation."""
    # Arrange
    router = TaskRouter()
    router_child = TaskRouter()
    router.include_router(router_child)

    # Act
    router_started_before = router.started()
    router_child_started_before = router_child.started()
    router.do_mark_as_started()
    router_started_after = router.started()
    router_child_started_after = router_child.started()

    # Assert
    assert router_started_before is False
    assert router_child_started_before is False
    assert router_started_after is True
    assert router_child_started_after is True
