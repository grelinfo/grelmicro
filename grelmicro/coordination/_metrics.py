"""Metric attributes and emit helpers shared by every coordination primitive.

A distributed lock fails quietly: the backend goes away, a lease is lost,
or every replica finds the lock already held, and the work simply stops
happening. These helpers make each of those a counter you can alert on.

Attribute mappings are built once per instance, so an emit on the acquire
path never builds a dict.
"""

from __future__ import annotations

from typing import Any

from grelmicro.metrics import _emit

ATTEMPTS = "grelmicro.lock.attempts"
"""Counter of backend acquire calls, one point per call."""

RENEWALS = "grelmicro.lock.renewals"
"""Counter of lease renewals, one point per call."""

HOLDERS = "grelmicro.lock.holders"
"""How many holders this worker has, which is 0 when it holds nothing."""

ACQUIRED = "acquired"
UNAVAILABLE = "unavailable"
ERROR = "error"
SUCCESS = "success"
LOST = "lost"


class LockMetrics:
    """The attribute mappings and emit calls for one lock instance."""

    __slots__ = ("_attempts", "_holders", "_renewals")

    def __init__(self, name: str, mode: str) -> None:
        """Bind the attributes for a lock named `name` held in `mode`."""
        base = {"grelmicro.lock.name": name, "grelmicro.lock.mode": mode}
        self._holders: dict[str, Any] = base
        self._attempts = {
            outcome: {**base, "grelmicro.outcome": outcome}
            for outcome in (ACQUIRED, UNAVAILABLE, ERROR)
        }
        self._renewals = {
            outcome: {**base, "grelmicro.outcome": outcome}
            for outcome in (SUCCESS, LOST, ERROR)
        }

    def attempt(self, outcome: str) -> None:
        """Count one backend acquire call, however it ended.

        A blocking acquire polls, so a lock under contention records one
        `unavailable` point per poll, which is the contention signal.
        """
        _emit.incr(ATTEMPTS, self._attempts[outcome], unit="{attempt}")

    def renewal(self, outcome: str) -> None:
        """Count one lease renewal, however it ended.

        `lost` is the one to alert on: the lease was gone before the work
        under it finished, so another worker may already hold the lock.
        """
        _emit.incr(RENEWALS, self._renewals[outcome], unit="{renewal}")

    def hold(self, amount: int) -> None:
        """Move the holder count by `amount`, which is 1 or -1.

        A read lease has several holders at once, so this counts them
        rather than reading as a flag. An exclusive lock reads 0 or 1.
        """
        _emit.add_up_down(HOLDERS, amount, self._holders, unit="{holder}")
