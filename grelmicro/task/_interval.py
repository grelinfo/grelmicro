"""Interval Task."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
from typing import Any, Literal

from grelmicro._async import sleep_or_stop
from grelmicro._task import Task
from grelmicro.coordination._protocol import LockPrimitive
from grelmicro.coordination._tokens import generate_worker_id
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig
from grelmicro.metrics import _emit
from grelmicro.task._fire import FireInfo, FireRecorder
from grelmicro.task._gate import LeaderWatch, check_sync, gate_label
from grelmicro.task._utils import validate_and_generate_reference

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
        seconds: float | timedelta,
        gate: Literal["claim"] | TaskLock | LeaderElection | None = None,
        sync: LockPrimitive | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If the gate lock already gates another task.
            SettingsValidationError: If the gate lock holds a claim for
                less than `seconds`.
            TypeError: If `gate` is not a supported value, or `sync` is
                a leader election.
        """
        seconds = (
            seconds.total_seconds()
            if isinstance(seconds, timedelta)
            else seconds
        )
        if seconds <= 0:
            msg = "seconds must be greater than 0"
            raise ValueError(msg)
        check_sync(sync)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._seconds = seconds
        self._function = function
        self._fire = FireRecorder(
            self._name, function, clock=partial(datetime.now, UTC)
        )

        self._gate_label = gate_label(gate, takes_lock=True)
        self._watch = (
            LeaderWatch(gate, self._name)
            if isinstance(gate, LeaderElection)
            else None
        )
        primitives = self._gate_primitives(gate, seconds)
        self._claim = next(
            (p for p in primitives if isinstance(p, TaskLock)), None
        )
        if sync is not None:
            primitives.append(sync)
        self._sync_primitives: list[LockPrimitive] = primitives

        self._last_loop_start: float | None = None

    def _gate_primitives(
        self,
        gate: Literal["claim"] | TaskLock | LeaderElection | None,
        seconds: float,
    ) -> list[LockPrimitive]:
        """Return the primitives a gate enters before the body, in order.

        A leader guard comes first, because it rejects a worker that is
        not the leader without touching the backend. The claim lock comes
        next, so it is held only once leadership is confirmed.
        """
        if gate is None:
            return []
        if isinstance(gate, TaskLock):
            gate._bind_task(  # noqa: SLF001
                self._name, interval=timedelta(seconds=seconds)
            )
            return [gate]
        if isinstance(gate, LeaderElection):
            return [gate.guard(), self._claim_lock(seconds)]
        return [self._claim_lock(seconds)]

    def _claim_lock(self, seconds: float) -> TaskLock:
        """Build the lock that holds one claim per interval.

        The claim is held for the whole interval. The task renews it
        while the body runs, so the lease of two intervals only bounds
        how long a crashed worker keeps it.
        The lock is built from a fixed config, so neither the environment
        nor an external reload retunes it.
        """
        interval = timedelta(seconds=seconds)
        lock = TaskLock.from_config(
            self._name,
            TaskLockConfig(
                worker=generate_worker_id(),
                min_hold_duration=interval,
                lease_duration=interval * 2,
            ),
        )
        lock._bind_task(self._name, interval=interval)  # noqa: SLF001
        return lock

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
            self._seconds,
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
                    time.time() + self._seconds,
                    self._fire.metric_attrs,
                    unit="s",
                )
                if await sleep_or_stop(self._seconds, stop):
                    break
        finally:
            logger.info("Task stopped: %s", self.name)

    def _schedule_delay(self) -> float | None:
        """Return how late the body starts against its planned instant.

        The interval is measured from the end of the previous iteration,
        so the planned instant is that moment plus `seconds`. It rises
        when a worker is saturated or when acquiring the lock takes
        longer than the interval it guards. Nothing was planned before
        the first iteration, which answers `None`.
        """
        planned = self._last_loop_start
        if planned is None:
            return None
        return max(time.monotonic() - (planned + self._seconds), 0.0)

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
            renewal = asyncio.create_task(self._renew_claim(primitive, done))
            try:
                await self._fire.claimed(
                    self._run_with_sync(primitives, index + 1)
                )
            finally:
                # Signal instead of cancel, so a renewal already sent to the
                # backend lands before the claim is released.
                done.set()
                await renewal

    async def _renew_claim(self, claim: TaskLock, done: asyncio.Event) -> None:
        """Keep the claim until `done` is set.

        Renews every third of the lease, read again before each wait so
        a `reconfigure` sets the pace. A renewal the backend fails is
        retried every tenth of the lease for as long as the lease since
        the last one that worked lasts. A claim the backend no longer
        holds stops the renewals. The body keeps running either way.
        """
        renewed_at = time.monotonic()
        failing = False
        while True:
            lease = claim.config.lease_duration.total_seconds()
            delay = lease / 10 if failing else lease / 3
            if await sleep_or_stop(delay, done):
                return
            try:
                await claim._renew_held()  # noqa: SLF001
            except LockNotOwnedError:
                logger.warning(
                    "Task lost its claim while the body ran: %s", self.name
                )
                return
            except Exception:
                if time.monotonic() - renewed_at >= lease:
                    logger.warning(
                        "Task could not renew its claim while the body ran: %s",
                        self.name,
                        exc_info=True,
                    )
                    return
                logger.debug(
                    "Task claim renewal failed, retrying: %s",
                    self.name,
                    exc_info=True,
                )
                failing = True
            else:
                renewed_at = time.monotonic()
                failing = False
