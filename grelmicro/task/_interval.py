"""Interval Task."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
from typing import TYPE_CHECKING, Any, Literal

from grelmicro._async import sleep_or_stop
from grelmicro._duration import check_duration
from grelmicro._task import Task
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.metrics import _emit
from grelmicro.task._fire import FireInfo, FireRecorder
from grelmicro.task._gate import check_sync, gate_label, spell_gate
from grelmicro.task._utils import validate_and_generate_reference

if TYPE_CHECKING:
    from grelmicro.coordination._protocol import LockPrimitive
    from grelmicro.coordination.leaderelection import LeaderElection
    from grelmicro.coordination.tasklock import TaskLock
    from grelmicro.task._gated import LeaderWatch

logger = getLogger("grelmicro.task")


class IntervalTask(Task):
    """Interval Task.

    Use the `Tasks.every()` or `TaskRouter.every()` decorator instead
    of creating IntervalTask objects directly.

    The ``gate`` decides which workers run each interval:

    - ``None``: every worker.
    - ``"claim"``: one worker claims each interval.
    - A ``TaskLock``: one worker claims each interval, with the lock's
      own tuning.
    - A ``LeaderElection``: the elected worker.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        interval: int | timedelta,
        gate: Literal["claim"] | TaskLock | LeaderElection | None = None,
        sync: LockPrimitive | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If `interval` is not whole seconds or a
                `timedelta`, is not greater than zero, or is over 100
                years.
            ValueError: If the gate lock already gates another task.
            SettingsValidationError: If the gate lock holds a claim for
                less than `interval`, or the gate locks under a `name` that
                is not a valid lock name or starts with `task-`.
            TypeError: If `gate` is not a supported value, or `sync` is
                a leader election.
        """
        interval = check_duration(interval, "interval")
        check_sync(sync)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._interval = interval
        self._interval_seconds = interval.total_seconds()
        self._function = function
        self._fire = FireRecorder(
            self._name, function, clock=partial(datetime.now, UTC)
        )

        gate = spell_gate(gate)
        self._gate = gate
        primitives: list[LockPrimitive] = []
        self._watch: LeaderWatch | None = None
        self._claim: TaskLock | None = None
        if gate is not None:
            from grelmicro.task._gated import interval_gate  # noqa: PLC0415

            primitives, self._watch, self._claim = interval_gate(
                self._name, gate, interval, derived=not name
            )
        # After `interval_gate`, which names an unnamed `TaskLock`.
        self._gate_label = gate_label(gate, takes_lock=True)
        if sync is not None:
            primitives.append(sync)
        self._sync_primitives = primitives

        self._last_loop_start: float | None = None

    @property
    def function(self) -> Callable[..., Any]:
        """The function the task runs, as it was registered.

        `Tasks.add_task` reads it to mark the function, so a decorator
        applied below the one that registered the task is refused
        rather than silently absent from every run.
        """
        return self._function

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    @property
    def gate(self) -> Literal["claim"] | TaskLock | LeaderElection | None:
        """The gate the task was built with.

        `"claim"` reads back as the string, a `TaskLock` or a
        `LeaderElection` as the object passed, and `None` when every
        worker runs it.
        """
        return self._gate

    @property
    def next_fire_time(self) -> datetime | None:
        """The computed next fire time based on last loop instant, or None when not started."""
        if self._last_loop_start is None:
            return None
        elapsed = timedelta(seconds=time.monotonic() - self._last_loop_start)
        return datetime.now(UTC) + max(self._interval - elapsed, timedelta(0))

    @property
    def last_fire(self) -> FireInfo | None:
        """The most recent fire info, or None before the first fire."""
        return self._fire.last

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run the repeated task loop."""
        logger.info(
            "Task started (interval: %ss, gate: %s): %s",
            self._interval_seconds,
            self._gate_label,
            self.name,
        )
        if ready is not None and not ready.done():  # pragma: no branch
            ready.set_result(None)
        watch = self._watch
        if watch is not None:
            watch.start()
        try:
            while True:
                await self._fire.guard(
                    self._run_with_sync(self._sync_primitives)
                )
                if watch is not None:
                    watch.check()
                # The current iteration finished. Break here on a graceful
                # stop so in-flight work is never interrupted; otherwise
                # sleep until the next interval (waking early on stop).
                self._last_loop_start = time.monotonic()
                _emit.observe(
                    "grelmicro.task.next_run",
                    time.time() + self._interval_seconds,
                    self._fire.metric_attrs,
                    unit="s",
                )
                if await sleep_or_stop(self._interval_seconds, stop):
                    break
        finally:
            logger.info("Task stopped: %s", self.name)

    def _schedule_delay(self) -> float | None:
        """Return how late the body starts against its planned instant.

        The interval is measured from the end of the previous iteration,
        so the planned instant is that moment plus `interval`. It rises
        when a worker is saturated or when acquiring the lock takes
        longer than the interval it guards. Nothing was planned before
        the first iteration, which answers `None`.
        """
        planned = self._last_loop_start
        if planned is None:
            return None
        return max(time.monotonic() - (planned + self._interval_seconds), 0.0)

    async def _run_with_sync(
        self, primitives: list[LockPrimitive], index: int = 0
    ) -> None:
        """Enter sync primitives via recursive nesting, then run the task.

        Using explicit recursion avoids AsyncExitStack and @asynccontextmanager
        overhead on every iteration.
        """
        if index >= len(primitives):
            fire = self._fire
            await fire.run(fire.now(), self._schedule_delay())
            return

        primitive = primitives[index]
        async with primitive:
            if primitive is not self._claim:
                await self._run_with_sync(primitives, index + 1)
                return
            # Renew from the moment the claim is held, so waiting for a
            # `sync` lock after it never lets the lease run out.
            done = asyncio.Event()
            extension = asyncio.create_task(self._extend_claim(primitive, done))
            try:
                await self._fire.claimed(
                    self._run_with_sync(primitives, index + 1)
                )
            finally:
                # Signal instead of cancel, so an extension already sent to the
                # backend lands before the claim is released.
                done.set()
                await extension

    async def _extend_claim(self, claim: TaskLock, done: asyncio.Event) -> None:
        """Keep the claim until `done` is set.

        Extends every third of the lease, read again before each wait so
        a `reconfigure` sets the pace. An extension the backend fails is
        retried every tenth of the lease for as long as the lease since
        the last one that worked lasts. A claim the backend no longer
        holds stops the extensions. The body keeps running either way.
        """
        extended_at = time.monotonic()
        failing = False
        while True:
            lease = claim.config.lease_duration.total_seconds()
            delay = lease / 10 if failing else lease / 3
            if await sleep_or_stop(delay, done):
                return
            try:
                await claim._extend_held()  # noqa: SLF001
            except LockNotOwnedError:
                logger.warning(
                    "Task lost its claim while the body ran: %s", self.name
                )
                return
            except Exception:
                if time.monotonic() - extended_at >= lease:
                    logger.warning(
                        "Task could not extend its claim while the body ran: %s",
                        self.name,
                        exc_info=True,
                    )
                    return
                logger.debug(
                    "Task claim extension failed, retrying: %s",
                    self.name,
                    exc_info=True,
                )
                failing = True
            else:
                extended_at = time.monotonic()
                failing = False
