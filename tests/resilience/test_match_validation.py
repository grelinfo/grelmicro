"""Type-validation tests for `Match.exception` / `Match.exception_cause`.

These pin the `isinstance(t, type) and issubclass(t, ...)` guard, so an
`and` to `or` flip (which would accept a non-exception type) is caught.
"""

from __future__ import annotations

import pytest

from grelmicro.resilience import Match


def test_exception_rejects_a_non_exception_type() -> None:
    """A plain class that is not an exception is rejected."""
    with pytest.raises(TypeError):
        Match.exception(int)  # ty: ignore[invalid-argument-type]


def test_exception_cause_rejects_a_non_exception_type() -> None:
    """A plain class that is not an exception is rejected for the cause form."""
    with pytest.raises(TypeError):
        Match.exception_cause(int)  # ty: ignore[invalid-argument-type]
