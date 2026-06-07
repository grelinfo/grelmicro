"""Tests for the `@measure` decorator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grelmicro.metrics import measure
from grelmicro.metrics._measure import _default_name

if TYPE_CHECKING:
    from tests.metrics.conftest import MetricsHarness


def test_default_name_drops_locals() -> None:
    """The default name lowercases module+qualname and drops `<locals>`."""

    def outer() -> None:
        def inner() -> None: ...

        assert "<locals>" not in _default_name(inner)
        assert _default_name(inner).endswith("outer.inner")

    outer()


def test_measure_noop_when_off() -> None:
    """A measured function runs normally when no component is active."""

    @measure
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5  # noqa: PLR2004


def test_measure_sync_success(metrics_reader: MetricsHarness) -> None:
    """Sync success records duration and a success-outcome call."""

    @measure(name="task")
    def work() -> str:
        return "done"

    assert work() == "done"
    calls = metrics_reader.points("task.calls")
    assert calls[0][1] == {"outcome": "success"}
    assert len(metrics_reader.points("task.duration")) == 1


def test_measure_sync_error(metrics_reader: MetricsHarness) -> None:
    """Sync error records an error outcome with the exception type."""

    @measure(name="task")
    def boom() -> None:
        raise ValueError

    with pytest.raises(ValueError):  # noqa: PT011
        boom()
    calls = metrics_reader.points("task.calls")
    assert calls[0][1] == {"outcome": "error", "error.type": "ValueError"}
    assert len(metrics_reader.points("task.duration")) == 1


async def test_measure_async_success(metrics_reader: MetricsHarness) -> None:
    """Async success records duration and a success call."""

    @measure(name="atask")
    async def work() -> str:
        return "ok"

    assert await work() == "ok"
    assert metrics_reader.points("atask.calls")[0][1] == {"outcome": "success"}
    assert len(metrics_reader.points("atask.duration")) == 1


async def test_measure_async_error(metrics_reader: MetricsHarness) -> None:
    """Async error records an error outcome with the exception type."""

    @measure(name="atask")
    async def boom() -> None:
        raise KeyError

    with pytest.raises(KeyError):
        await boom()
    assert metrics_reader.points("atask.calls")[0][1] == {
        "outcome": "error",
        "error.type": "KeyError",
    }


def test_measure_in_flight_sync(metrics_reader: MetricsHarness) -> None:
    """`record_in_flight` nets to zero after a sync call returns."""

    @measure(name="task", record_in_flight=True)
    def work() -> None: ...

    work()
    assert metrics_reader.points("task.active")[0][0] == 0


async def test_measure_in_flight_async(metrics_reader: MetricsHarness) -> None:
    """`record_in_flight` nets to zero after an async call raises."""

    @measure(name="atask", record_in_flight=True)
    async def boom() -> None:
        raise RuntimeError

    with pytest.raises(RuntimeError):
        await boom()
    assert metrics_reader.points("atask.active")[0][0] == 0


def test_measure_default_name(metrics_reader: MetricsHarness) -> None:
    """A bare `@measure` derives the name from the function."""

    @measure
    def labeled() -> None: ...

    labeled()
    names = metrics_reader.collect().keys()
    assert any(n.endswith("labeled.duration") for n in names)
