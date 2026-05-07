"""Tests for the Grelmicro app container and Module protocol."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, Self

import pytest

if TYPE_CHECKING:
    from types import TracebackType

from grelmicro import (
    Grelmicro,
    Module,
    ModuleAlreadyRegisteredError,
    ModuleNotRegisteredError,
    NoActiveAppError,
)
from grelmicro.errors import OutOfContextError

_BOOM = "boom"
_RAISED = "raised"


class _RecordingModule:
    """A Module implementation that records its enter/exit lifecycle."""

    kind: ClassVar[str] = "rec"

    def __init__(
        self, *, name: str = "default", log: list[str] | None = None
    ) -> None:
        self.name = name
        self.log: list[str] = log if log is not None else []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        self.log.append(f"enter:{self.name}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self.exited += 1
        self.log.append(f"exit:{self.name}")
        return None


class _OtherModule(_RecordingModule):
    """Different `kind` so it can coexist with `_RecordingModule` on `micro`."""

    kind: ClassVar[str] = "oth"


class _RaisingModule(_RecordingModule):
    """Raises on `__aenter__` so we can test partial-startup cleanup."""

    async def __aenter__(self) -> Self:
        self.log.append(f"enter:{self.name}")
        raise RuntimeError(_BOOM)


# --- Module protocol ---


def test_module_protocol_is_runtime_checkable() -> None:
    """A class with kind/name/__aenter__/__aexit__ satisfies the Module protocol."""
    assert isinstance(_RecordingModule(), Module)


# --- .use() registration ---


def test_use_attaches_module_on_kind_attr() -> None:
    """`.use()` exposes the module as `micro.<kind>`."""
    micro = Grelmicro()
    pattern = _RecordingModule()
    micro.use(pattern)
    assert micro.rec is pattern


def test_use_returns_the_module() -> None:
    """`.use()` returns the feature so callers can keep a typed reference."""
    micro = Grelmicro()
    pattern = _RecordingModule()
    returned = micro.use(pattern)
    assert returned is pattern


def test_use_same_instance_is_noop() -> None:
    """Re-registering the exact same instance under the same name is a no-op."""
    micro = Grelmicro()
    pattern = _RecordingModule()
    micro.use(pattern)
    micro.use(pattern)


def test_use_different_instance_same_key_raises() -> None:
    """Two different instances under the same `(kind, name)` raises."""
    micro = Grelmicro()
    micro.use(_RecordingModule())
    with pytest.raises(ModuleAlreadyRegisteredError):
        micro.use(_RecordingModule())


def test_use_same_kind_different_name_coexists() -> None:
    """Multiple modules of the same kind under different names coexist."""
    micro = Grelmicro()
    primary = _RecordingModule(name="primary")
    analytics = _RecordingModule(name="analytics")
    micro.use(primary)
    micro.use(analytics)
    assert micro.get("rec", "primary") is primary
    assert micro.get("rec", "analytics") is analytics


# --- modules= constructor ---


def test_modules_kwarg_registers_in_order() -> None:
    """`Grelmicro(modules=[...])` is equivalent to repeated `.use(...)` calls."""
    a = _RecordingModule(name="a")
    b = _OtherModule(name="b")
    micro = Grelmicro(modules=[a, b])
    assert micro.get("rec", "a") is a
    assert micro.get("oth", "b") is b


def test_modules_kwarg_accepts_none() -> None:
    """`modules=None` (the default) constructs an empty container."""
    micro = Grelmicro()
    with pytest.raises(ModuleNotRegisteredError):
        micro.get("rec")


# --- Lifespan: enter, LIFO teardown ---


async def test_lifespan_enters_and_exits_in_lifo_order() -> None:
    """Modules enter in registration order, exit in reverse."""
    log: list[str] = []
    a = _RecordingModule(name="a", log=log)
    b = _RecordingModule(name="b", log=log)
    c = _RecordingModule(name="c", log=log)
    micro = Grelmicro(modules=[a, b, c])
    async with micro:
        assert log == ["enter:a", "enter:b", "enter:c"]
    assert log == [
        "enter:a",
        "enter:b",
        "enter:c",
        "exit:c",
        "exit:b",
        "exit:a",
    ]


async def test_lifespan_partial_startup_failure_unwinds_already_entered() -> (
    None
):
    """A failure in module N rolls back modules 0..N-1 in LIFO order."""
    log: list[str] = []
    good = _RecordingModule(name="good", log=log)
    bad = _RaisingModule(name="bad", log=log)
    micro = Grelmicro(modules=[good, bad])
    with pytest.raises(RuntimeError, match=_BOOM):
        async with micro:
            pass
    assert log == ["enter:good", "enter:bad", "exit:good"]


async def test_lifespan_can_be_reentered_after_clean_exit() -> None:
    """A `Grelmicro` instance can be opened again after closing cleanly."""
    micro = Grelmicro(modules=[_RecordingModule()])
    async with micro:
        pass
    async with micro:
        pass


# --- ContextVar ambient lookup ---


async def test_current_micro_returns_active_app_inside_block() -> None:
    """`Grelmicro.current()` returns the active `Grelmicro` inside `async with`."""
    micro = Grelmicro()
    async with micro:
        assert Grelmicro.current() is micro


async def test_current_micro_raises_outside_block() -> None:
    """`Grelmicro.current()` raises `NoActiveAppError` outside any active app."""
    with pytest.raises(NoActiveAppError):
        Grelmicro.current()


async def test_current_micro_is_per_task() -> None:
    """Two concurrent tasks each see their own Grelmicro."""
    micro_a = Grelmicro()
    micro_b = Grelmicro()
    seen: dict[str, Grelmicro] = {}

    async def run(label: str, micro: Grelmicro) -> None:
        async with micro:
            await asyncio.sleep(0)  # let the other task interleave
            seen[label] = Grelmicro.current()

    await asyncio.gather(run("a", micro_a), run("b", micro_b))
    assert seen["a"] is micro_a
    assert seen["b"] is micro_b


# --- override() ---


async def test_override_swaps_module_for_block() -> None:
    """`micro.override(...)` replaces a module for the duration of the block."""
    log: list[str] = []
    real = _RecordingModule(name="default", log=log)
    mock = _RecordingModule(name="default", log=[])
    mock.log = log  # share log
    micro = Grelmicro(modules=[real])
    async with micro:
        async with micro.override(mock):
            assert micro.rec is mock
        assert micro.rec is real
    # mock entered and exited inside the override block
    assert "enter:default" in log


async def test_double_aenter_raises() -> None:
    """Re-entering an already-open `Grelmicro` raises `OutOfContextError`."""
    micro = Grelmicro()
    async with micro:
        with pytest.raises(OutOfContextError):
            await micro.__aenter__()


async def test_aexit_without_aenter_raises() -> None:
    """Calling `__aexit__` on a never-entered `Grelmicro` raises `OutOfContextError`."""
    micro = Grelmicro()
    with pytest.raises(OutOfContextError):
        await micro.__aexit__(None, None, None)


async def test_module_aexit_can_resolve_current_micro() -> None:
    """Modules consulting `Grelmicro.current()` from `__aexit__` see the active app."""
    seen: list[Grelmicro] = []

    class _CurrentLookupModule:
        kind: ClassVar[str] = "rec"

        def __init__(self) -> None:
            self.name = "default"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            seen.append(Grelmicro.current())
            return None

    micro = Grelmicro(modules=[_CurrentLookupModule()])
    async with micro:
        pass
    assert seen == [micro]


async def test_override_outside_active_context_raises() -> None:
    """`override(...)` outside an active `async with micro:` raises `OutOfContextError`."""
    micro = Grelmicro()
    mock = _RecordingModule(name="default")
    with pytest.raises(OutOfContextError):
        async with micro.override(mock):
            pass


async def test_unknown_kind_attribute_raises_attribute_error() -> None:
    """`micro.<unknown_kind>` raises a regular `AttributeError`."""
    micro = Grelmicro()
    with pytest.raises(AttributeError, match="no module of kind 'nope'"):
        _ = micro.nope


async def test_override_restores_on_exception() -> None:
    """`override(...)` restores prior registrations even when the block raises."""
    real = _RecordingModule(name="default")
    mock = _RecordingModule(name="default")
    micro = Grelmicro(modules=[real])
    async with micro:
        with pytest.raises(RuntimeError, match=_RAISED):
            async with micro.override(mock):
                raise RuntimeError(_RAISED)
        assert micro.rec is real
