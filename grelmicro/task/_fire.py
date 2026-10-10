"""One fire of a scheduled task, recorded the same way for every kind of task.

`IntervalTask` and `CronTask` decide differently when a fire is due and which
worker takes it. Once one is due, they run and record it here, so a fire
reads the same in `last_fire` and in the metrics whatever scheduled it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from logging import getLogger
from typing import TYPE_CHECKING, Any

from fast_depends import inject

from grelmicro._async import is_async_callable
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.errors import WouldBlockError
from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

logger = getLogger("grelmicro.task")


class FireOutcome(StrEnum):
    """Outcome of a task fire.

    - ``SUCCESS``: the body ran and returned without raising.
    - ``ERROR``: the body raised an exception.
    - ``SKIPPED``: another worker handled the fire, so this one stood
      down. The peer either ran it or recorded it as missed.
    - ``MISSED``: the fire was dropped and no worker ran it. Either it
      came back too late to replay, past ``misfire_grace``, or
      this worker claimed it and then could not admit the body.
    - ``COORDINATION_ERROR``: the fire never reached the body because
      coordination itself failed, such as an unreachable schedule backend
      or a lock acquire that raised.
    """

    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSED = "missed"
    COORDINATION_ERROR = "coordination_error"


@dataclass(frozen=True)
class FireInfo:
    """Information about a task fire."""

    started_at: datetime
    outcome: FireOutcome
    duration: float


class FireRecorder:
    """Runs a task's body and records every fire, run or not.

    Built once per task. `last` is the most recent fire, and `started`
    says whether the body started on the current one.
    """

    __slots__ = (
        "_clock",
        "_function",
        "_name",
        "last",
        "metric_attrs",
        "started",
    )

    def __init__(
        self,
        name: str,
        function: Callable[..., Any],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        """Record the fires of the task `name`, timestamped by `clock`.

        `function` is injected once, and a synchronous one runs in a worker
        thread.
        """
        self._name = name
        self._clock = clock
        injected = inject(function)
        self._function: Callable[[], Awaitable[Any]] = (
            injected
            if is_async_callable(injected)
            else partial(asyncio.to_thread, injected)
        )
        self.metric_attrs: dict[str, Any] = {"grelmicro.task.name": name}
        """The attributes of every point that carries only the task name."""
        self.last: FireInfo | None = None
        self.started = False

    def now(self) -> datetime:
        """Return the current time on the task's clock."""
        return self._clock()

    def unrun(
        self,
        started_at: datetime,
        outcome: FireOutcome,
        error: Exception | None = None,
    ) -> None:
        """Record a fire that never reached the body.

        Emits one `grelmicro.task.runs` point and sets `last` together, so
        the counter and `last_fire` never disagree about a fire.
        """
        attributes: dict[str, Any] = {
            "grelmicro.task.name": self._name,
            "grelmicro.outcome": outcome,
        }
        if error is not None:
            attributes["error.type"] = type(error).__name__
        _emit.incr("grelmicro.task.runs", attributes, unit="{run}")
        self.last = FireInfo(
            started_at=started_at, outcome=outcome, duration=0.0
        )

    async def run(self, started_at: datetime, delay: float | None) -> None:
        """Run the body once and record the fire.

        `delay` is how late the body starts against its planned instant, in
        seconds, or `None` when nothing was planned. A body that raises is
        recorded as an error and does not propagate. A cancelled body is
        recorded as an error too, and the cancellation propagates.
        """
        self.started = True
        _emit.add_up_down(
            "grelmicro.task.active", 1, self.metric_attrs, unit="{run}"
        )
        if delay is not None:
            _emit.record_duration(
                "grelmicro.task.schedule.delay", delay, self.metric_attrs
            )
        start_monotonic = time.perf_counter()
        outcome = FireOutcome.ERROR
        # Assumes failure until the body returns, so a fire cancelled
        # mid-body records its duration as the error it was.
        attributes: dict[str, Any] = {
            "grelmicro.task.name": self._name,
            "grelmicro.outcome": FireOutcome.ERROR,
        }
        try:
            await self._function()
            outcome = FireOutcome.SUCCESS
            attributes["grelmicro.outcome"] = FireOutcome.SUCCESS
            _emit.incr("grelmicro.task.runs", attributes, unit="{run}")
        except Exception as exc:
            logger.exception("Task execution error: %s", self._name)
            attributes["error.type"] = type(exc).__name__
            _emit.incr("grelmicro.task.runs", attributes, unit="{run}")
        finally:
            duration = time.perf_counter() - start_monotonic
            self.last = FireInfo(
                started_at=started_at, outcome=outcome, duration=duration
            )
            _emit.record_duration(
                "grelmicro.task.duration", duration, attributes
            )
            _emit.add_up_down(
                "grelmicro.task.active", -1, self.metric_attrs, unit="{run}"
            )

    async def claimed(
        self, admission: Awaitable[None], at: datetime | None = None
    ) -> None:
        """Await the rest of a fire this worker claimed.

        The claim stops every peer from running the fire, so a `sync`
        primitive refusing to admit the body loses it outright. That is
        recorded as a miss at `at`, now by default, and not as a skip.
        """
        try:
            await admission
        except WouldBlockError:
            logger.warning(
                "Task fire missed, claimed but not admitted: %s", self._name
            )
            self.unrun(self.now() if at is None else at, FireOutcome.MISSED)

    async def guard(self, fire: Awaitable[None]) -> None:
        """Await one fire, recording what kept it from the body.

        A refused admission is a skip. A lock that ran out while the body
        ran is logged, since the body already recorded its fire. Any other
        failure is a coordination error when the body never started, and
        is only logged when it did. Cancellation always propagates.
        """
        self.started = False
        try:
            await fire
        except asyncio.CancelledError:
            raise
        except WouldBlockError as exc:
            self.unrun(self.now(), FireOutcome.SKIPPED)
            logger.debug("Task skipped: %s (%s)", self._name, exc)
        except LockNotOwnedError:
            # The lock expired on release, so the body already ran and
            # already reported its own outcome. Counting it again would
            # double the fire.
            logger.warning(
                "Task released a lock it no longer held: %s."
                " Its lease ran out while the body ran.",
                self._name,
            )
        except Exception as exc:
            logger.exception("Task synchronization error: %s", self._name)
            if not self.started:
                self.unrun(self.now(), FireOutcome.COORDINATION_ERROR, exc)
        # Re-raise pending cancellation that an inner cleanup may have
        # shadowed with a regular Exception.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            task.uncancel()
            raise asyncio.CancelledError
