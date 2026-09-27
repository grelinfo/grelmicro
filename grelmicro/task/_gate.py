"""Which workers run a scheduled task, checked the same way for every kind."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.coordination.leaderelection import LeaderElection, _LeaderGuard
from grelmicro.coordination.tasklock import TaskLock

if TYPE_CHECKING:
    from grelmicro.coordination._protocol import LockPrimitive

__all__ = ["check_sync", "gate_label"]


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
