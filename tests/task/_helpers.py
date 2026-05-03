"""Shared test helpers for task and sync tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

ReadyTask = Callable[..., Coroutine[Any, Any, Any]]


async def start_task(
    tg: asyncio.TaskGroup,
    task: ReadyTask,
    *args: Any,  # noqa: ANN401
    **kwargs: Any,  # noqa: ANN401
) -> asyncio.Task[Any]:
    """Schedule ``task(ready=Future, *args, **kwargs)`` and await readiness."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    handle = tg.create_task(task(*args, ready=ready, **kwargs))
    await ready
    return handle


def cancel_group(tg: asyncio.TaskGroup) -> None:
    """Cancel every still-running child task in the group."""
    for child in list(tg._tasks):
        child.cancel()
