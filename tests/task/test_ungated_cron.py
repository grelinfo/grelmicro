"""Test the report on a cron task that leaves `gate` out beside a schedule backend."""

import asyncio
import functools
import logging
import warnings
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Any, Self

import pytest

from grelmicro import Grelmicro, UngatedCronWarning, Usable
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.task import TaskRouter, Tasks
from grelmicro.task._cron import CronTask
from grelmicro.types import Environment

pytestmark = [pytest.mark.timeout(10)]

NAME = "tests.task.test_ungated_cron:cleanup"
"""The name the cron task takes from `cleanup`."""


async def cleanup() -> None:
    """Do nothing, on a cron schedule."""


def _coordination() -> Coordination:
    """Return a memory Coordination that holds a schedule backend."""
    return Coordination(
        lock=MemoryLockAdapter(),
        schedule=MemoryScheduleAdapter(),
        requires="process",
    )


def _app(*uses: Usable, environment: Environment = "production") -> Grelmicro:
    """Build an app in `environment`, `production` unless told otherwise."""
    return Grelmicro(uses=list(uses), environment=environment)


def _tasks(**gate: Any) -> Tasks:  # noqa: ANN401
    """Return a Tasks running `cleanup` at 03:00, with `gate` if passed."""
    tasks = Tasks()
    tasks.cron("0 3 * * *", **gate)(cleanup)
    return tasks


@contextmanager
def _reports() -> Generator[list[warnings.WarningMessage]]:
    """Record every ungated-cron warning instead of raising it."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UngatedCronWarning)
        yield caught


def _ungated(caught: list[warnings.WarningMessage]) -> list[str]:
    """Return the messages of the ungated-cron warnings in `caught`."""
    return [str(w.message) for w in caught if w.category is UngatedCronWarning]


async def _open(micro: Grelmicro) -> None:
    """Open `micro` long enough for its tasks to start."""
    async with micro:
        await asyncio.sleep(0)


async def test_gate_left_out_beside_a_schedule_backend_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The task names itself and both gates that silence the report."""
    micro = _app(_coordination(), _tasks())

    with _reports() as caught:
        await _open(micro)

    [message] = _ungated(caught)
    assert f"{NAME!r}" in message
    assert "every worker" in message
    assert "gate='claim'" in message
    assert "gate=None" in message
    assert "[ungated-cron]" in message
    [record] = [
        r
        for r in caplog.records
        if getattr(r, "diagnostic", None) == "ungated-cron"
    ]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == message


async def test_gate_none_passed_does_not_warn() -> None:
    """`gate=None` says every worker runs each fire on purpose."""
    micro = _app(_coordination(), _tasks(gate=None))

    with _reports() as caught:
        await _open(micro)

    assert _ungated(caught) == []


async def test_gate_claim_does_not_warn() -> None:
    """A gated cron task runs each fire once, so nothing is reported."""
    micro = _app(_coordination(), _tasks(gate="claim"))

    with _reports() as caught:
        await _open(micro)

    assert _ungated(caught) == []


async def test_gate_left_out_without_a_schedule_backend_does_not_warn() -> None:
    """Without a backend there is no claim to point at."""
    micro = _app(_tasks())

    with _reports() as caught:
        await _open(micro)

    assert _ungated(caught) == []


async def test_tasks_outside_an_app_does_not_warn() -> None:
    """A Tasks opened on its own resolves no backend."""
    with _reports() as caught:
        async with _tasks():
            await asyncio.sleep(0)

    assert _ungated(caught) == []


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_gate_left_out_is_quiet_in_development_and_test(
    environment: Environment,
) -> None:
    """One process runs there, so every worker is one worker."""
    micro = _app(_coordination(), _tasks(), environment=environment)

    with _reports() as caught:
        await _open(micro)

    assert _ungated(caught) == []


async def test_warning_as_error_does_not_end_the_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A filter set to `error` keeps the task running, and the log keeps the report."""
    tasks = _tasks()
    micro = _app(_coordination(), tasks)

    with warnings.catch_warnings():
        warnings.simplefilter("error", UngatedCronWarning)
        async with micro:
            await asyncio.sleep(0)
            [task] = tasks.tasks
            assert isinstance(task, CronTask)
            assert task.next_fire_time is not None

    assert [
        r
        for r in caplog.records
        if getattr(r, "diagnostic", None) == "ungated-cron"
    ]


def test_describe_reports_the_gate_left_out() -> None:
    """`grelmicro check` names the task, as a warning."""
    micro = _app(_coordination(), _tasks())

    [check] = [c for c in micro.describe().checks if c.name == "ungated-cron"]

    assert check.status == "warn"
    assert f"{NAME!r}" in check.detail
    assert "gate='claim'" in check.detail
    assert micro.describe().ok


def test_describe_reports_a_task_from_an_included_router() -> None:
    """A cron task registered on a router the Tasks includes is checked too."""
    router = TaskRouter()
    router.cron("0 3 * * *")(cleanup)
    tasks = Tasks()
    tasks.include_router(router)
    micro = _app(_coordination(), tasks)

    [check] = [c for c in micro.describe().checks if c.name == "ungated-cron"]

    assert check.status == "warn"
    assert f"{NAME!r}" in check.detail


@pytest.mark.parametrize("environment", ["development", "test"])
def test_describe_passes_the_gate_left_out_in_development_and_test(
    environment: Environment,
) -> None:
    """`grelmicro check` is quiet where the startup report is quiet."""
    micro = _app(_coordination(), _tasks(), environment=environment)

    [check] = [c for c in micro.describe().checks if c.name == "ungated-cron"]

    assert check.status == "ok"
    assert repr(environment) in check.detail


def _logged(
    function: Callable[[], Awaitable[None]],
) -> Callable[[], Awaitable[None]]:
    """Wrap `function` the way a decorator of your own would."""

    @functools.wraps(function)
    async def wrapper() -> None:
        await function()

    return wrapper


async def _wrapped() -> None:
    """Do nothing, under a decorator of your own."""


wrapped = _logged(_wrapped)
"""`_wrapped` under `_logged`, registered as a cron task."""


class _CallableJob:
    """A callable object with no `__code__`, registered as a cron task."""

    def __init__(self) -> None:
        """Name the job as a module-level function would be named."""
        self.__qualname__ = "callable_job"

    async def __call__(self) -> None:
        """Do nothing."""


def _recorded(caught: list[warnings.WarningMessage]) -> warnings.WarningMessage:
    """Return the one ungated-cron warning in `caught`."""
    [report] = [w for w in caught if w.category is UngatedCronWarning]
    return report


async def test_warning_points_at_the_cron_function() -> None:
    """The location is the function to add `gate=` to."""
    with _reports() as caught:
        await _open(_app(_coordination(), _tasks()))

    report = _recorded(caught)
    assert report.filename == cleanup.__code__.co_filename
    assert report.lineno == cleanup.__code__.co_firstlineno


async def test_warning_points_through_a_wrapping_decorator() -> None:
    """A `functools.wraps` decorator is unwrapped to the function it wraps."""
    tasks = Tasks()
    tasks.cron("0 3 * * *")(wrapped)

    with _reports() as caught:
        await _open(_app(_coordination(), tasks))

    report = _recorded(caught)
    assert report.filename == _wrapped.__code__.co_filename
    assert report.lineno == _wrapped.__code__.co_firstlineno


async def test_warning_on_a_callable_without_code_still_warns() -> None:
    """A callable object has no definition to point at, and still warns."""
    tasks = Tasks()
    tasks.cron("0 3 * * *")(_CallableJob())

    with _reports() as caught:
        await _open(_app(_coordination(), tasks))

    assert "'tests.task.test_ungated_cron:callable_job'" in str(
        _recorded(caught).message
    )


async def test_module_filter_silences_the_warning() -> None:
    """A filter on the module that defines the task silences it."""
    with _reports() as caught:
        warnings.filterwarnings(
            "ignore",
            category=UngatedCronWarning,
            module=r"tests\.task\.test_ungated_cron",
        )
        await _open(_app(_coordination(), _tasks()))

    assert _ungated(caught) == []


@pytest.mark.parametrize("gate", [None, "claim"])
def test_describe_passes_every_named_gate(gate: str | None) -> None:
    """A cron task that names its gate is reported as passing."""
    micro = _app(_coordination(), _tasks(gate=gate))

    [check] = [c for c in micro.describe().checks if c.name == "ungated-cron"]

    assert check.status == "ok"


def test_describe_skips_the_check_without_a_schedule_backend() -> None:
    """Without a backend, no row is reported."""
    micro = _app(_tasks())

    assert [
        c for c in micro.describe().checks if c.name == "ungated-cron"
    ] == []


def test_describe_skips_the_check_without_a_cron_task() -> None:
    """A schedule backend alone reports no row."""
    micro = _app(_coordination(), Tasks())

    assert [
        c for c in micro.describe().checks if c.name == "ungated-cron"
    ] == []


async def test_gate_left_out_logs_as_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cron task with no gate starts on every worker, logged as `none`."""
    caplog.set_level(logging.INFO, logger="grelmicro.task")

    with _reports():
        await _open(_app(_coordination(), _tasks()))

    assert f"gate: none): {NAME}" in caplog.text


def test_backend_without_gate_is_refused() -> None:
    """`backend=` is read only by a gated task, whether `gate` was left out or not."""
    with pytest.raises(ValueError, match="gate='claim'"):
        CronTask(
            function=cleanup,
            expr="0 3 * * *",
            backend=MemoryScheduleAdapter(),
        )


class _OwnCoordination:
    """A component of the `coordination` kind that is not grelmicro's."""

    kind = "coordination"
    name = "default"

    async def __aenter__(self) -> Self:
        """Open nothing."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close nothing."""


@pytest.mark.parametrize(
    "coordination",
    [
        Coordination(lock=MemoryLockAdapter(), requires="process"),
        _OwnCoordination(),
    ],
    ids=["no-schedule", "own-kind"],
)
async def test_gate_left_out_without_a_schedule_is_not_reported(
    coordination: Usable,
) -> None:
    """A coordination with no schedule backend has no claim to point at."""
    micro = _app(coordination, _tasks())

    with _reports() as caught:
        await _open(micro)

    assert _ungated(caught) == []
    assert [
        c for c in micro.describe().checks if c.name == "ungated-cron"
    ] == []
