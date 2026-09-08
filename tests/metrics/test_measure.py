"""Tests for the `@measure` decorator."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

import pytest

from grelmicro.metrics import measure
from grelmicro.metrics._measure import _default_name, _Instruments
from grelmicro.metrics._naming import metric_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.metrics.conftest import MetricsHarness


SLEEP = 0.02
"""Long enough that a timed body reads apart from an untimed one."""


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
    assert calls[0][1] == {"grelmicro.outcome": "success"}
    assert len(metrics_reader.points("task.duration")) == 1


def test_measure_sync_error(metrics_reader: MetricsHarness) -> None:
    """Sync error records an error outcome with the exception type."""

    @measure(name="task")
    def boom() -> None:
        raise ValueError

    with pytest.raises(ValueError):  # noqa: PT011
        boom()
    calls = metrics_reader.points("task.calls")
    assert calls[0][1] == {
        "grelmicro.outcome": "error",
        "error.type": "ValueError",
    }
    assert len(metrics_reader.points("task.duration")) == 1


async def test_measure_async_success(metrics_reader: MetricsHarness) -> None:
    """Async success records duration and a success call."""

    @measure(name="atask")
    async def work() -> str:
        return "ok"

    assert await work() == "ok"
    assert metrics_reader.points("atask.calls")[0][1] == {
        "grelmicro.outcome": "success"
    }
    assert len(metrics_reader.points("atask.duration")) == 1


async def test_measure_async_error(metrics_reader: MetricsHarness) -> None:
    """Async error records an error outcome with the exception type."""

    @measure(name="atask")
    async def boom() -> None:
        raise KeyError

    with pytest.raises(KeyError):
        await boom()
    assert metrics_reader.points("atask.calls")[0][1] == {
        "grelmicro.outcome": "error",
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


def test_a_partial_is_named_after_the_function_it_wraps() -> None:
    """A metric name never carries a memory address.

    A `functools.partial` has no name of its own and reports `functools`
    as its module, so the old fallback put its `repr` in the metric name.
    That repr carries the address of the wrapped function, which is a new
    metric on every restart of the process.
    """
    bound = functools.partial(_sample, "p")

    name = _default_name(bound)

    assert name == _default_name(_sample)
    assert "0x" not in name


def test_a_callable_object_is_named_after_its_type() -> None:
    """A callable with no `__qualname__` falls back to a stable name."""

    class Fetcher:
        async def __call__(self) -> None: ...

    name = _default_name(Fetcher())

    assert name.endswith("fetcher")
    assert "0x" not in name


async def _sample(prefix: str, value: int) -> str:
    """Module-level sample for the naming tests."""
    return f"{prefix}{value}"


def test_a_callable_object_keeps_the_module_of_its_class() -> None:
    """A callable instance keeps the module of its class.

    An instance carries no `__module__` of its own, but attribute lookup
    falls back to its class, which does. The module is part of the name
    for a callable object exactly as it is for a function, so two types
    of the same name in different modules stay apart.
    """
    name = _default_name(_Fetcher())

    assert name == f"{__name__}._fetcher".lower()


class _Fetcher:
    """Callable sample for the naming tests."""

    async def __call__(self) -> None:
        """Do nothing."""


def test_two_nested_classes_of_one_name_stay_apart() -> None:
    """The type's qualified name is what disambiguates them.

    The module cannot: both are defined in this one. Taking the bare
    `__name__` would merge their two metrics into one.
    """

    def factory() -> Callable[[], Awaitable[None]]:
        class _Fetcher:
            async def __call__(self) -> None: ...

        return _Fetcher()

    assert _default_name(factory()) != _default_name(_Fetcher())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("__main__.charge", "main.charge"),
        ("__main__.<lambda>", "main.lambda"),
        ("<lambda>", "lambda"),
        ("___", "unnamed"),
        ("myapp.service.charge", "myapp.service.charge"),
        ("myapp._internal.foo", "myapp._internal.foo"),
    ],
)
def test_a_derived_metric_name_is_one_opentelemetry_accepts(
    raw: str, expected: str
) -> None:
    """An instrument name starts with a letter and holds no brackets.

    The last two cases are already valid and must come back untouched,
    because renaming a metric that works today would break the dashboard
    reading it.
    """
    assert metric_name(raw) == expected


def test_measuring_a_function_in_the_entry_point_script_does_not_raise(
    metrics_reader: MetricsHarness,
) -> None:
    """`@measure` never breaks the call it was added to watch.

    A function defined in the script the process was started from has
    the module `__main__`, and a name starting with an underscore is
    refused by the SDK. It raised from inside the call, and only once
    metrics were turned on, so an app that ran in development failed in
    production.
    """
    instruments = _Instruments("__main__.charge", in_flight=False)

    instruments.exit(instruments.enter(), instruments.success())

    assert metrics_reader.points("main.charge.duration")


def test_an_explicit_name_the_sdk_refuses_does_not_raise(
    metrics_reader: MetricsHarness,
) -> None:
    """A name passed by hand is corrected rather than allowed to crash.

    `@measure` is observability, so it never breaks the call it wraps,
    whatever it was named. A name the specification already accepts is
    left exactly as it was, which the parametrised test above pins.
    """

    @measure(name="2 orders/sec")
    def charge() -> int:
        return 1

    assert charge() == 1
    assert metrics_reader.points("orders/sec.calls")


async def test_measuring_an_async_callable_object_times_the_body(
    metrics_reader: MetricsHarness,
) -> None:
    """The duration covers the body, and a failure reads as one.

    The sync wrapper timed the construction of the coroutine, which is
    immediate, and recorded `success` before the body had run. A body
    that then raised was never counted as an error.
    """

    class Slow:
        async def __call__(self) -> str:
            await asyncio.sleep(SLEEP)
            return "ok"

    class Boom:
        async def __call__(self) -> str:
            msg = "boom"
            raise ValueError(msg)

    slow, boom = Slow(), Boom()
    assert await measure(slow)() == "ok"
    with pytest.raises(ValueError, match="boom"):
        await measure(boom)()

    duration = metrics_reader.points(f"{_default_name(slow)}.duration")
    assert duration[0][0] >= SLEEP
    calls = metrics_reader.points(f"{_default_name(boom)}.calls")
    assert calls[0][1]["grelmicro.outcome"] == "error"
    assert calls[0][1]["error.type"] == "ValueError"
