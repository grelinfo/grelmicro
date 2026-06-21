"""Timing-boundary tests for leader election.

These pin the renew-deadline and confirmation-age comparisons at their exact
boundary, so a `>=` to `>` or `<=` to `<` flip is caught.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import grelmicro.coordination.leaderelection as le_module
from grelmicro.coordination.leaderelection import LeaderElection

if TYPE_CHECKING:
    import pytest

_UPDATED_AT = 100.0
_CONFIRMED_AT = 100.0
_MAX_AGE = 5.0


def test_renew_deadline_reached_at_exact_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renew is due at exactly `renew_deadline` seconds (the guard is `>=`)."""
    election = LeaderElection("renew-boundary")
    config = election._config
    election._state_updated_at = _UPDATED_AT
    monkeypatch.setattr(
        le_module, "monotonic", lambda: _UPDATED_AT + config.renew_deadline
    )

    assert election._is_renew_deadline_reached(config) is True


def test_confirmed_within_at_exact_max_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmation is valid at exactly `max_age` seconds old (`<=`)."""
    election = LeaderElection("confirm-boundary")
    election._is_leader = True
    election._last_confirmed_at = _CONFIRMED_AT
    monkeypatch.setattr(
        le_module, "monotonic", lambda: _CONFIRMED_AT + _MAX_AGE
    )

    assert election.is_leader_confirmed_within(_MAX_AGE) is True
