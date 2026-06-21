"""Boundary tests for coordination clusters left by the earlier campaign.

These pin the memory fencing-token counter, the leader `last_confirmation_age`
sign, and the lock-name length boundary, so a flipped operator or off-by-one
in those spots is caught.
"""

from __future__ import annotations

import pytest

import grelmicro.coordination.leaderelection as le_module
import grelmicro.coordination.memory as mem_module
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter

_NAME_MAX_LEN = 200
_DURATION = 60.0
_CONFIRMED_AT = 100.0
_NOW = 105.0
_SECOND_TOKEN = 2


async def test_fence_token_starts_at_one_and_climbs() -> None:
    """The fencing token starts at 1 and increments on each free-to-held."""
    async with MemoryLockAdapter() as backend:
        first = await backend.acquire(name="x", token="a", duration=_DURATION)
        assert first == 1

        await backend.release(name="x", token="a")
        second = await backend.acquire(name="x", token="b", duration=_DURATION)
        assert second == _SECOND_TOKEN


def test_last_confirmation_age_is_now_minus_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`last_confirmation_age` is `now - last_confirmed`, not their sum."""
    election = LeaderElection("le-age")
    election._last_confirmed_at = _CONFIRMED_AT
    monkeypatch.setattr(le_module, "monotonic", lambda: _NOW)

    assert election.last_confirmation_age() == _NOW - _CONFIRMED_AT


def test_lock_name_at_max_length_is_valid() -> None:
    """A name of exactly the maximum length is valid; one over is rejected."""
    Lock("a" * _NAME_MAX_LEN)  # no raise at the boundary

    with pytest.raises(ValueError, match="at most"):
        Lock("a" * (_NAME_MAX_LEN + 1))


async def test_lock_is_still_held_at_the_exact_expiry_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `now == expire_at` the lock is still held (the guard is `>=`)."""
    clock = {"t": _CONFIRMED_AT}
    monkeypatch.setattr(mem_module, "monotonic", lambda: clock["t"])

    async with MemoryLockAdapter() as backend:
        await backend.acquire(name="x", token="a", duration=_DURATION)
        clock["t"] = _CONFIRMED_AT + _DURATION  # exactly the expiry instant

        assert await backend.locked(name="x") is True
        assert await backend.owned(name="x", token="a") is True
        assert await backend.release(name="x", token="a") is True
