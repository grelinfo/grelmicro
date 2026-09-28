"""Which workers run a scheduled task, checked the same way for every kind."""

from __future__ import annotations

import warnings
from contextlib import suppress
from logging import getLogger
from typing import TYPE_CHECKING

from grelmicro._diagnostics import LEADER_NOT_RUNNING, diagnostic
from grelmicro.clock import monotonic
from grelmicro.coordination.leaderelection import LeaderElection, _LeaderGuard
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import LeaderNotRunningWarning

if TYPE_CHECKING:
    from grelmicro.coordination._protocol import LockPrimitive

__all__ = ["LeaderWatch", "check_sync", "gate_label"]

logger = getLogger("grelmicro.task")


def gate_label(gate: object, *, takes_lock: bool) -> str:
    """Return how the task logs `gate`, once it is one the task takes.

    Every kind of task takes `None`, `"claim"` and a `LeaderElection`. A
    `TaskLock` is taken only when `takes_lock` is set.

    Raises:
        TypeError: If `gate` is none of those.
    """
    if gate is None:
        return "none"
    if gate == "claim":
        return "claim"
    if isinstance(gate, LeaderElection):
        return f"LeaderElection({gate.name!r})"
    if takes_lock and isinstance(gate, TaskLock):
        return f"TaskLock({gate.name!r})"
    accepted = "'claim', a TaskLock or" if takes_lock else "'claim' or"
    msg = f"gate must be None, {accepted} a LeaderElection, got {gate!r}"
    raise TypeError(msg)


def check_sync(sync: LockPrimitive | None) -> None:
    """Refuse a leader election passed as `sync`.

    Raises:
        TypeError: If `sync` is a `LeaderElection` or its guard.
    """
    if isinstance(sync, LeaderElection | _LeaderGuard):
        msg = "sync takes a resource lock, pass a LeaderElection as gate="
        raise TypeError(msg)


class LeaderWatch:
    """Report a task gated on a leader election that does not run.

    An election acquires leadership only while it runs, so the task it
    gates skips every fire. The watch reports it once, on a fire at least
    one lease duration after the task started, when the election has not
    run at any fire it saw. An election that runs at a fire settles the
    watch, and so does the report.
    """

    __slots__ = ("_election", "_settled", "_since", "_task")

    def __init__(self, election: LeaderElection, task: str) -> None:
        """Watch `election` for the task named `task`."""
        self._election = election
        self._task = task
        self._since: float | None = None
        self._settled = False

    def start(self) -> None:
        """Mark the moment the task started."""
        self._since = monotonic()

    def check(self) -> None:
        """Report the election once when it has not run for a lease duration.

        The report is a log record on the `grelmicro.task` logger and a
        `LeaderNotRunningWarning`, both carrying the `leader-not-running`
        code. A warnings filter set to `error` never raises it into the
        task.
        """
        since = self._since
        if self._settled or since is None:
            return
        election = self._election
        if election.is_running():
            self._settled = True
            return
        if monotonic() - since < election.config.lease_duration:
            return
        self._settled = True
        msg = diagnostic(
            LEADER_NOT_RUNNING,
            f"Task {self._task!r} is gated on the leader election "
            f"{election.name!r}, which has not run since the task started, "
            f"so the task skips every fire. Register it with "
            f"tasks.add_task(election), or start it.",
        )
        logger.warning(msg, extra={"diagnostic": LEADER_NOT_RUNNING})
        # A filter that turns the warning into an error must not end the
        # task loop. The log record above still carries the report.
        with suppress(LeaderNotRunningWarning):
            warnings.warn(msg, LeaderNotRunningWarning, stacklevel=2)
