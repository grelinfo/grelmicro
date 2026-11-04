"""Test what `@tasks.job` refuses at registration."""

import asyncio
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

import pytest

from grelmicro.resilience import retry
from grelmicro.task import Job, Progress, Tasks
from grelmicro.task.errors import TaskAddOperationError
from grelmicro.task.router import TaskRouter

pytestmark = [pytest.mark.timeout(10)]


async def _noop(_progress: Progress) -> None:
    """Do nothing."""


async def _no_progress(upload_id: str) -> None:
    """Take no `progress`."""


async def _progress_second(upload_id: str, progress: Progress) -> None:
    """Take `progress` after another parameter."""


async def _nothing() -> None:
    """Take no parameter at all."""


async def _typing_only(progress: _types.Progress) -> None:  # type: ignore[name-defined]  # ty: ignore[unresolved-reference]  # noqa: F821
    """Take `progress` annotated with a name only a type checker imports."""


async def _stringized(progress: "Progress") -> None:  # noqa: UP037
    """Take `progress` annotated as a string, as `from __future__` writes it."""


async def _annotated(progress: Annotated[Progress, "reports"]) -> None:
    """Take `progress` wrapped in `Annotated`."""


async def _annotated_string(
    progress: "Annotated[Progress, 'reports']",  # noqa: UP037
) -> None:
    """Take `progress` wrapped in `Annotated`, as a string."""


async def _typing_only_argument(
    progress: Progress,
    upload: "_types.Upload",  # type: ignore[name-defined]  # ty: ignore[unresolved-reference]  # noqa: F821, UP037
) -> None:
    """Take an argument typed with a name imported only for type checkers."""


class _Other:
    """Another library."""

    class Progress:
        """A class that shares the name `Progress`."""


async def _other_progress(progress: "_Other.Progress") -> None:  # noqa: UP037
    """Take another library's `Progress`."""


async def _unannotated(progress) -> None:  # noqa: ANN001
    """Take an unannotated `progress`."""


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param({"max_concurrent": 0}, id="max_concurrent-zero"),
        pytest.param({"max_queued": -1}, id="max_queued-negative"),
        pytest.param({"max_concurrent": 1.5}, id="max_concurrent-float"),
        pytest.param({"max_concurrent": True}, id="max_concurrent-bool"),
        pytest.param({"max_queued": 1.5}, id="max_queued-float"),
        pytest.param({"max_queued": True}, id="max_queued-bool"),
        pytest.param({"keep": -1}, id="keep-negative"),
        pytest.param({"max_wait": -1}, id="max_wait-negative"),
        pytest.param({"max_wait": 1.5}, id="max_wait-float"),
        pytest.param({"max_wait": 0}, id="max_wait-zero"),
        pytest.param({"keep": 1.5}, id="keep-float"),
        pytest.param({"keep": timedelta(days=365 * 101)}, id="keep-too-long"),
    ],
)
def test_job_register_refuses_a_bad_limit(limits: dict[str, Any]) -> None:
    """A limit out of range raises `ValueError` where it is written."""
    # Arrange
    tasks = Tasks()

    # Act & Assert
    with pytest.raises(ValueError, match=next(iter(limits))):
        tasks.job(**limits)(_noop)


async def _takes_an_event(_progress: Progress, gate: asyncio.Event) -> None:
    """Take an argument with no JSON form."""


def test_job_register_refuses_an_argument_type_with_no_json_form() -> None:
    """A parameter that cannot travel as JSON raises where the job is written."""
    # Arrange
    tasks = Tasks()

    # Act & Assert
    with pytest.raises(TypeError, match="gate"):
        tasks.job()(_takes_an_event)


@pytest.mark.parametrize(
    "function",
    [
        pytest.param(_no_progress, id="no-progress"),
        pytest.param(_progress_second, id="progress-second"),
        pytest.param(_other_progress, id="another-progress-class"),
        pytest.param(_nothing, id="no-parameter"),
    ],
)
def test_job_register_refuses_a_body_without_progress_first(
    function: Callable[..., Any],
) -> None:
    """The body must take `progress: Progress` as its first parameter."""
    # Arrange
    tasks = Tasks()

    # Act & Assert
    with pytest.raises(TypeError, match="progress"):
        tasks.job()(function)


async def test_job_register_refused_after_the_tasks_started() -> None:
    """A job registered once the tasks run raises `TaskAddOperationError`."""
    # Arrange
    tasks = Tasks()

    async with tasks:
        # Act & Assert
        with pytest.raises(TaskAddOperationError):
            tasks.job()(_noop)


def test_job_register_refuses_a_decorator_above_it() -> None:
    """A grelmicro decorator above `@tasks.job` refuses, naming the order."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_noop)

    # Act & Assert
    with pytest.raises(TypeError, match="registered"):
        retry(when=Exception, attempts=3)(job)


@pytest.mark.parametrize(
    "function",
    [
        pytest.param(_stringized, id="string-annotation"),
        pytest.param(_unannotated, id="unannotated"),
        pytest.param(_annotated, id="annotated"),
        pytest.param(_annotated_string, id="annotated-string"),
        pytest.param(_typing_only_argument, id="typing-only-argument"),
    ],
)
def test_job_register_accepts_progress_written_another_way(
    function: Callable[..., Any],
) -> None:
    """A string annotation, or a bare `progress`, passes the check."""
    # Arrange
    tasks = Tasks()

    # Act
    job = tasks.job()(function)

    # Assert
    assert job.name.endswith(getattr(function, "__qualname__", ""))


def test_job_register_raises_name_error_for_an_unresolvable_annotation() -> (
    None
):
    """An annotation only a type checker can read fails as it does for tasks.

    The body's dependencies are read when the job is built, so the name
    that does not exist at runtime is what the error reports.
    """
    # Arrange
    tasks = Tasks()

    # Act & Assert
    with pytest.raises(NameError, match="_types"):
        tasks.job()(_typing_only)


_ONE_DAY = 86400
"""The default of `max_wait` and `keep`, in seconds."""


@pytest.mark.parametrize(
    "written",
    [
        pytest.param(Job.__init__, id="job"),
        pytest.param(TaskRouter.job, id="decorator"),
    ],
)
def test_job_limits_default_to_no_queue_limit_and_one_day(
    written: Callable[..., Any],
) -> None:
    """A job has no queue limit, and waits and stays readable for a day."""
    # Act
    parameters = inspect.signature(written).parameters

    # Assert
    assert parameters["max_queued"].default is None
    assert parameters["max_wait"].default == _ONE_DAY
    assert parameters["keep"].default == _ONE_DAY
