"""Interval Task."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
from typing import Any
from uuid import UUID

from fast_depends import inject

from grelmicro._async import is_async_callable, sleep_or_stop
from grelmicro.coordination.abc import LockBackend, LockPrimitive
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import WouldBlockError
from grelmicro.metrics import _emit
from grelmicro.task._cron import FireInfo, FireOutcome
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.abc import Task

logger = getLogger("grelmicro.task")


class IntervalTask(Task):
    """Interval Task.

    Use the `Tasks.interval()` or `TaskRouter.interval()` decorator instead
    of creating IntervalTask objects directly.

    Supports three modes:
    - Local: No lock params, runs on every worker.
    - Distributed lock: Set ``lease_duration`` to enable at-most-once per interval.
    - Leader-gated: Set ``leader`` to restrict execution to the leader worker.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        seconds: float | timedelta,
        lease_duration: float | None = None,
        min_hold_duration: float | None = None,
        leader: LeaderElection | None = None,
        backend: LockBackend | None = None,
        worker: str | UUID | None = None,
        sync: LockPrimitive | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If lease_duration is less than seconds.
            ValueError: If min_hold_duration is set without lease_duration or leader.
            ValueError: If min_hold_duration is greater than lease_duration.
        """
        seconds = (
            seconds.total_seconds()
            if isinstance(seconds, timedelta)
            else seconds
        )
        if seconds <= 0:
            msg = "seconds must be greater than 0"
            raise ValueError(msg)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._seconds = seconds
        self._async_function = self._prepare_async_function(function)

        distributed = lease_duration is not None or leader is not None

        if min_hold_duration is not None and not distributed:
            msg = (
                "min_hold_duration requires lease_duration or leader to be set"
            )
            raise ValueError(msg)

        if distributed:
            resolved_lease_duration = (
                lease_duration if lease_duration is not None else seconds * 5
            )
            resolved_min_hold_duration = (
                min_hold_duration if min_hold_duration is not None else seconds
            )

            if resolved_lease_duration < seconds:
                msg = "lease_duration must be greater than or equal to seconds"
                raise ValueError(msg)

            if resolved_min_hold_duration > resolved_lease_duration:
                msg = (
                    "min_hold_duration must be less than or equal to "
                    "lease_duration"
                )
                raise ValueError(msg)

            task_lock = TaskLock(
                self._name,
                backend=backend,
                worker=worker,
                min_hold_duration=resolved_min_hold_duration,
                lease_duration=resolved_lease_duration,
            )
            self._sync_primitives: list[LockPrimitive] = _build_sync_list(
                leader=leader,
                task_lock=task_lock,
                resource_lock=sync,
            )
        elif sync is not None:
            self._sync_primitives = [sync]
        else:
            self._sync_primitives = []

        self._last_fire: FireInfo | None = None
        self._last_loop_start: float | None = None

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    @property
    def next_fire_time(self) -> datetime | None:
        """The computed next fire time based on last loop instant, or None when not started."""
        if self._last_loop_start is None:
            return None
        elapsed = time.monotonic() - self._last_loop_start
        remaining = max(self._seconds - elapsed, 0)
        return datetime.now(UTC) + timedelta(seconds=remaining)

    @property
    def last_fire(self) -> FireInfo | None:
        """The most recent fire info, or None before the first fire."""
        return self._last_fire

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run the repeated task loop."""
        logger.info(
            "Task started (interval: %ss): %s", self._seconds, self.name
        )
        if ready is not None and not ready.done():  # pragma: no branch
            ready.set_result(None)
        try:
            while True:
                try:
                    await self._run_with_sync(self._sync_primitives)
                except asyncio.CancelledError:
                    raise
                except WouldBlockError as exc:
                    self._last_fire = FireInfo(
                        started_at=datetime.now(UTC),
                        outcome=FireOutcome.SKIPPED,
                        duration=0.0,
                    )
                    logger.debug("Task skipped: %s (%s)", self.name, exc)
                except LockNotOwnedError:
                    logger.warning(
                        "Task took too long and lock expired: %s."
                        " Consider increasing lease_duration.",
                        self.name,
                    )
                except Exception:
                    logger.exception(
                        "Task synchronization error: %s", self.name
                    )
                # Re-raise pending cancellation that an inner cleanup
                # may have shadowed with a regular Exception.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    task.uncancel()
                    raise asyncio.CancelledError
                # The current iteration finished. Break here on a graceful
                # stop so in-flight work is never interrupted; otherwise
                # sleep until the next interval (waking early on stop).
                self._last_loop_start = time.monotonic()
                if await sleep_or_stop(self._seconds, stop):
                    break
        finally:
            logger.info("Task stopped: %s", self.name)

    async def _run_with_sync(
        self, primitives: list[LockPrimitive], index: int = 0
    ) -> None:
        """Enter sync primitives via recursive nesting, then run the task.

        Using explicit recursion avoids AsyncExitStack and @asynccontextmanager
        overhead on every iteration.
        """
        if index >= len(primitives):
            _emit.add_up_down(
                "grelmicro.task.active", 1, **{"task.name": self.name}
            )
            started_at = datetime.now(UTC)
            start_monotonic = time.perf_counter()
            outcome = FireOutcome.ERROR
            try:
                await self._async_function()
                outcome = FireOutcome.SUCCESS
                _emit.incr(
                    "grelmicro.task.runs",
                    **{"task.name": self.name, "outcome": FireOutcome.SUCCESS},
                )
            except Exception as exc:
                logger.exception("Task execution error: %s", self.name)
                _emit.incr(
                    "grelmicro.task.runs",
                    **{
                        "task.name": self.name,
                        "outcome": FireOutcome.ERROR,
                        "error.type": type(exc).__name__,
                    },
                )
            finally:
                duration = time.perf_counter() - start_monotonic
                self._last_fire = FireInfo(
                    started_at=started_at,
                    outcome=outcome,
                    duration=duration,
                )
                _emit.record_duration(
                    "grelmicro.task.duration",
                    duration,
                    **{"task.name": self.name},
                )
                _emit.add_up_down(
                    "grelmicro.task.active", -1, **{"task.name": self.name}
                )
            return

        async with primitives[index]:
            await self._run_with_sync(primitives, index + 1)

    def _prepare_async_function(
        self, function: Callable[..., Any]
    ) -> Callable[..., Awaitable[Any]]:
        """Prepare the function with lock and ensure async function."""
        function = inject(function)
        return (
            function
            if is_async_callable(function)
            else partial(asyncio.to_thread, function)
        )


def _build_sync_list(
    *,
    leader: LeaderElection | None,
    task_lock: TaskLock,
    resource_lock: LockPrimitive | None = None,
) -> list[LockPrimitive]:
    """Build an ordered list of sync primitives.

    Acquisition order (outermost to innermost):

    1. **Leader guard**: Cheapest check; instantly rejects non-leader workers
       without touching any lock, avoiding unnecessary contention.
    2. **Task lock**: The distributed ``TaskLock`` with TTL that guarantees
       at-most-once execution per interval. Acquired after leadership is
       confirmed to keep the TTL window tight.
    3. **Resource lock**: A user-provided ``Lock`` for shared-resource access.
       Acquired last so the resource is held only during actual execution,
       minimizing contention on the shared resource.
    """
    primitives: list[LockPrimitive] = []
    if leader is not None:
        primitives.append(leader.guard())
    primitives.append(task_lock)
    if resource_lock is not None:
        primitives.append(resource_lock)
    return primitives
