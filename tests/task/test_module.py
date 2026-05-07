"""Tests for the `Tasks` module (Grelmicro app integration)."""

from __future__ import annotations

import asyncio

from grelmicro import Grelmicro, Module
from grelmicro.task import Tasks

# Counter shared with module-level scheduled tasks (the `interval` decorator
# rejects nested / lambda callables, so the function must live at module scope).
_runs = 0
_MIN_RUNS = 2


async def _tick() -> None:
    """Increment the module-level counter."""
    global _runs  # noqa: PLW0603
    _runs += 1


def test_tasks_satisfies_module_protocol() -> None:
    """`Tasks` is a runtime-checkable `Module`."""
    assert isinstance(Tasks(), Module)


def test_tasks_default_kind_and_name() -> None:
    """Default kind is `task` and default name is `default`."""
    tasks = Tasks()
    assert tasks.kind == "task"
    assert tasks.name == "default"


def test_tasks_constructor_does_not_start_manager() -> None:
    """Constructor is pure: the underlying TaskManager is not opened."""
    tasks = Tasks()
    assert tasks.manager._task_group is None


def test_tasks_named_registration() -> None:
    """A named `Tasks` pattern can coexist with the default one."""
    micro = Grelmicro(modules=[Tasks(), Tasks(name="analytics")])
    assert micro.get("task", "default").name == "default"
    assert micro.get("task", "analytics").name == "analytics"


async def test_tasks_opens_and_closes_manager_with_app() -> None:
    """`async with micro:` opens and closes the underlying TaskManager."""
    tasks = Tasks()
    micro = Grelmicro(modules=[tasks])
    async with micro:
        assert tasks.manager._task_group is not None
    # On exit, the TaskManager exit stack tore down the TaskGroup.


async def test_tasks_interval_decorator_runs_task() -> None:
    """A task scheduled via `tasks.interval(...)` runs while the app is open."""
    global _runs  # noqa: PLW0603
    _runs = 0
    tasks = Tasks()
    tasks.interval(seconds=0.01)(_tick)
    micro = Grelmicro(modules=[tasks])
    async with micro:
        await asyncio.sleep(0.05)
    assert _runs >= _MIN_RUNS


async def test_tasks_accessible_via_micro_kind_attribute() -> None:
    """The `Tasks` pattern is exposed as `micro.task`."""
    tasks = Tasks()
    micro = Grelmicro(modules=[tasks])
    assert micro.task is tasks


async def test_tasks_interval_decorator_via_micro_attribute() -> None:
    """`@micro.task.interval(...)` is the conventional decorator path."""
    global _runs  # noqa: PLW0603
    _runs = 0
    micro = Grelmicro(modules=[Tasks()])
    micro.task.interval(seconds=0.01)(_tick)
    async with micro:
        await asyncio.sleep(0.05)
    assert _runs >= _MIN_RUNS


def test_tasks_add_task_forwards_to_manager() -> None:
    """`add_task` forwards to the underlying TaskManager."""
    tasks = Tasks()
    intermediate = Tasks()
    intermediate.interval(seconds=1)(_tick)
    # Take the IntervalTask the helper just registered and re-add it via
    # the public `add_task` forwarder. If the forward works, no error.
    [interval_task] = intermediate.manager.tasks
    tasks.add_task(interval_task)
    assert interval_task in tasks.manager.tasks
