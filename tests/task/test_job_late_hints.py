"""Test a job whose string type hints name something defined after the job."""

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from grelmicro.task import Job, JobInfo, JobStatus, Progress, Tasks

if TYPE_CHECKING:
    from decimal import Decimal as _CheckedOnly

pytestmark = [pytest.mark.timeout(10)]


class _Early(BaseModel):
    """A model defined before the jobs that name it."""

    a: int


class _NoJson:
    """A class with no JSON form, defined before the jobs that name it."""


_tasks = Tasks()


@_tasks.job()
async def _bare(progress: Progress, row: "_Late") -> str:  # noqa: UP037
    """Take a model defined below."""
    _ = progress
    return type(row).__name__


@_tasks.job()
async def _nested_string(progress: Progress, rows: list["_Late"]) -> str:  # noqa: UP037
    """Take a list of a model defined below, written as a string."""
    _ = progress
    return type(rows[0]).__name__


@_tasks.job()
async def _whole_string(progress: Progress, rows: "list[_Late]") -> str:  # noqa: UP037
    """Take a list of a model defined below, written whole as a string."""
    _ = progress
    return type(rows[0]).__name__


@_tasks.job()
async def _union_string(progress: Progress, row: "_Late | None") -> str:  # noqa: UP037
    """Take an optional model defined below, written as a string."""
    _ = progress
    return type(row).__name__


@_tasks.job()
async def _variadic(progress: Progress, *rows: "_Late") -> str:  # noqa: UP037
    """Take any number of a model defined below."""
    _ = progress
    return type(rows[0]).__name__


@_tasks.job()
async def _nested_early(progress: Progress, rows: list["_Early"]) -> str:  # noqa: UP037
    """Take a list of a model already defined, written as a string."""
    _ = progress
    return type(rows[0]).__name__


@_tasks.job()
async def _no_json(progress: Progress, thing: "_LateNoJson") -> None:  # noqa: UP037
    """Take a class with no JSON form, defined below."""
    _ = progress, thing


@_tasks.job()
async def _checked_only(progress: Progress, rows: list["_CheckedOnly"]) -> str:  # noqa: UP037
    """Take a nested name only a type checker can resolve."""
    _ = progress
    return type(rows).__name__


async def _nested_no_json(progress: Progress, things: list["_NoJson"]) -> None:  # noqa: UP037
    """Take a list of a class with no JSON form, written as a string."""
    _ = progress, things


class _Late(BaseModel):
    """A model defined after the jobs that name it."""

    a: int


class _LateNoJson:
    """A class with no JSON form, defined after the job that names it."""


async def _ended(job: Job[...], job_id: str) -> JobInfo:
    """Poll `job_id` until it reaches an end state, and return it."""
    while True:
        info = await job.without_owner().get(job_id)
        assert info is not None
        if info.finished_at is not None:
            return info
        await asyncio.sleep(0)


async def test_job_hints_resolve_once_the_module_is_loaded() -> None:
    """A hint defined after the job rebuilds its type, at every nesting."""
    # Arrange
    calls: list[tuple[Job[...], object, str]] = [
        (_bare, _Late(a=1), "_Late"),
        (_bare, {"a": 1}, "_Late"),
        (_nested_string, [_Late(a=1)], "_Late"),
        (_whole_string, [_Late(a=1)], "_Late"),
        (_union_string, _Late(a=1), "_Late"),
        (_variadic, _Late(a=1), "_Late"),
        (_nested_early, [_Early(a=1)], "_Early"),
        (_checked_only, [1, 2], "list"),
    ]

    # Act
    async with _tasks:
        ended = [
            await _ended(job, (await job.without_owner().submit(argument)).id)
            for job, argument, _ in calls
        ]

        # Assert
        with pytest.raises(TypeError, match="does not fit"):
            await _bare.without_owner().submit("no model")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="has no JSON form"):
            await _no_json.without_owner().submit(_LateNoJson())

    assert [(info.status, info.result) for info in ended] == [
        (JobStatus.SUCCEEDED, result) for _, _, result in calls
    ]


def test_job_nested_hint_with_no_json_form_raises_where_it_is_written() -> None:
    """A nested type with no JSON form fails at once when it resolves."""
    # Act
    with pytest.raises(TypeError, match="has no JSON form"):
        Tasks().job()(_nested_no_json)


def test_job_hint_builds_its_adapter_once() -> None:
    """A hint that resolves at first use is not resolved again on the next."""
    # Arrange
    arguments = _bare._arguments

    # Act
    first = arguments._adapter_of("row")

    # Assert
    assert arguments._adapter_of("row") is first
