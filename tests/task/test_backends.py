"""Task Manager registry, scoped overrides, and lifespan wiring."""

from collections.abc import Generator

import pytest

import grelmicro
from grelmicro import task as task_mod
from grelmicro._backends import (
    BackendAlreadyRegisteredError,
    BackendNotLoadedError,
)
from grelmicro.task import TaskManager
from grelmicro.task._backends import get_task_manager, task_manager_registry


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """Reset the task manager registry between tests."""
    task_manager_registry.reset()
    yield
    task_manager_registry.reset()


def test_constructor_does_not_register() -> None:
    """Constructing a TaskManager performs no registry writes."""
    TaskManager()
    assert not task_manager_registry.is_loaded


def test_get_not_loaded() -> None:
    """`get_task_manager` raises when no manager has been registered."""
    with pytest.raises(BackendNotLoadedError):
        get_task_manager()


def test_register_and_get() -> None:
    """Register and resolve."""
    manager = TaskManager()
    task_mod.register(manager)
    assert get_task_manager() is manager


def test_use_manager_registers_default() -> None:
    """`use_manager` registers under the default name."""
    manager = TaskManager()
    task_mod.use_manager(manager)
    assert get_task_manager() is manager


def test_register_same_instance_is_noop() -> None:
    """Re-registering the same instance under the same name is a no-op."""
    manager = TaskManager()
    task_mod.register(manager)
    task_mod.register(manager)
    assert get_task_manager() is manager


def test_register_different_instance_raises() -> None:
    """Registering a different instance under the same name raises."""
    task_mod.register(TaskManager())
    with pytest.raises(BackendAlreadyRegisteredError):
        task_mod.register(TaskManager())


def test_unregister_with_identity_check() -> None:
    """`unregister` clears only when the identity matches."""
    a = TaskManager()
    b = TaskManager()
    task_mod.register(a)
    task_mod.unregister(manager=b)  # wrong instance: no-op
    assert get_task_manager() is a
    task_mod.unregister(manager=a)
    assert not task_manager_registry.is_loaded


def test_use_overrides_default() -> None:
    """`use` overrides the default slot for the block."""
    registered = TaskManager()
    override = TaskManager()
    task_mod.register(registered)
    with task_mod.use(override):
        assert get_task_manager() is override
    assert get_task_manager() is registered


def test_use_overrides_named() -> None:
    """`use` overrides a named entry for the block."""
    primary = TaskManager()
    fake = TaskManager()
    task_mod.register(primary, "primary")
    with task_mod.use(primary=fake):
        assert get_task_manager("primary") is fake
    assert get_task_manager("primary") is primary


async def test_lifespan_starts_and_stops_registered_manager() -> None:
    """`grelmicro.lifespan()` enters the registered manager."""
    manager = TaskManager()
    task_mod.use_manager(manager)
    async with grelmicro.lifespan():
        assert manager._task_group is not None


async def test_lifespan_excludes_task_module() -> None:
    """`exclude={"task"}` skips the task registry."""
    manager = TaskManager()
    task_mod.use_manager(manager)
    async with grelmicro.lifespan(exclude={"task"}):
        assert manager._task_group is None
