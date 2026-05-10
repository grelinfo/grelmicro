"""Outcome dataclass coverage."""

from __future__ import annotations

import pytest

from grelmicro.resilience import Outcome


def test_from_exception_sets_raised() -> None:
    """``from_exception`` builds a raised outcome."""
    exc = ValueError("boom")
    outcome = Outcome.from_exception(exc)
    assert outcome.raised
    assert outcome.exception is exc
    assert outcome.result is None


def test_from_result_sets_returned() -> None:
    """``from_result`` builds a returned outcome."""
    outcome = Outcome.from_result(42)
    assert not outcome.raised
    assert outcome.result == 42  # noqa: PLR2004
    assert outcome.exception is None


def test_outcome_is_frozen() -> None:
    """``Outcome`` instances are immutable."""
    outcome = Outcome.from_result(1)
    with pytest.raises(AttributeError):
        outcome.raised = True  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_outcome_with_none_result() -> None:
    """``None`` result is allowed and round-trips."""
    outcome: Outcome[int | None] = Outcome.from_result(None)
    assert outcome.result is None
    assert not outcome.raised


def test_outcome_with_false_result() -> None:
    """``False`` result is preserved (not coerced to None)."""
    outcome = Outcome.from_result(False)  # noqa: FBT003
    assert outcome.result is False
    assert not outcome.raised
