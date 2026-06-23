"""Test Task Router."""

import re
from datetime import timedelta
from functools import partial

import pytest

from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.task import TaskRouter
from grelmicro.task._interval import IntervalTask
from grelmicro.task.errors import FunctionTypeError, TaskAddOperationError
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
    sync = Lock(backend=MemoryLockAdapter(), name="testlock")

    # Act
    router.interval(name="test1", seconds=10, sync=sync)(test1)
    router.interval(name="test2", seconds=20)(test2)
    router.interval(seconds=10)(test3)

    # Assert
    assert len(router.tasks) == task_count
    assert (
        sum(isinstance(task, IntervalTask) for task in router.tasks)
        == task_count - 1
    )
    assert router.tasks[0].name == "event_task"
    assert router.tasks[1].name == "test1"
    assert router.tasks[2].name == "test2"
    assert router.tasks[3].name == "tests.task.samples:test3"


def test_router_interval_with_timedelta() -> None:
    """Test Task Router add interval task with a timedelta interval."""
    # Arrange
    router = TaskRouter()
    interval = timedelta(minutes=2)
    seconds = 5

    # Act
    router.interval(seconds=interval)(test1)
    router.interval(seconds=seconds)(test2)

    # Assert
    assert isinstance(router.tasks[0], IntervalTask)
    assert router.tasks[0]._seconds == interval.total_seconds()
    assert isinstance(router.tasks[1], IntervalTask)
    assert router.tasks[1]._seconds == seconds


def test_router_interval_name_generation() -> None:
    """Test Task Router Interval Name Generation."""
    # Arrange
    router = TaskRouter()

    # Act
    router.interval(seconds=10)(test1)
    router.interval(seconds=10)(SimpleClass.static_method)
    router.interval(seconds=10)(SimpleClass.method)

    # Assert
    assert router.tasks[0].name == "tests.task.samples:test1"
    assert (
        router.tasks[1].name == "tests.task.samples:SimpleClass.static_method"
    )
    assert router.tasks[2].name == "tests.task.samples:SimpleClass.method"


def test_router_interval_name_generation_error() -> None:
    """Test Task Router Interval Name Generation Error."""
    # Arrange
    router = TaskRouter()
    test_instance = SimpleClass()

    # Act
    with pytest.raises(FunctionTypeError, match="nested function"):

        @router.interval(seconds=10)
        def nested_function() -> None:
            pass

    with pytest.raises(FunctionTypeError, match="lambda"):
        router.interval(seconds=10)(lambda _: None)

    with pytest.raises(FunctionTypeError, match="method"):
        router.interval(seconds=10)(test_instance.method)

    with pytest.raises(FunctionTypeError, match=re.escape("partial()")):
        router.interval(seconds=10)(partial(test1))

    with pytest.raises(
        FunctionTypeError,
        match="callable without __module__ or __qualname__ attribute",
    ):
        router.interval(seconds=10)(object())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_router_interval_with_lock() -> None:
    """Test Task Router add interval task with distributed lock."""
    # Arrange
    backend = MemoryLockAdapter()
    router = TaskRouter()

    # Act
    router.interval(seconds=60, lease_duration=300, backend=backend)(test1)

    # Assert
    assert len(router.tasks) == 1
    assert isinstance(router.tasks[0], IntervalTask)
    assert router.tasks[0].name == "tests.task.samples:test1"


def test_router_interval_with_lock_and_custom_least() -> None:
    """Test Task Router add interval task with custom min_hold_duration."""
    # Arrange
    backend = MemoryLockAdapter()
    router = TaskRouter()

    # Act
    router.interval(
        seconds=60,
        lease_duration=300,
        min_hold_duration=30,
        backend=backend,
    )(test1)

    # Assert
    assert len(router.tasks) == 1
    assert isinstance(router.tasks[0], IntervalTask)


def test_router_add_task_when_started() -> None:
    """Test Task Router Add Task When Started."""
    # Arrange
    custom_task = EventTask()
    router = TaskRouter()
    router.do_mark_as_started()

    # Act
    with pytest.raises(TaskAddOperationError):
        router.add_task(custom_task)


def test_router_include_router_when_started() -> None:
    """Test Task Router Include Router When Started."""
    # Arrange
    router = TaskRouter()
    router.do_mark_as_started()
    router_child = TaskRouter()

    # Act
    with pytest.raises(TaskAddOperationError):
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
