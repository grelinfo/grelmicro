"""Who the caller of a request is, read the same way by every reader."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grelmicro._caller import is_authenticated, subject_of


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (SimpleNamespace(is_authenticated=True), True),
        (SimpleNamespace(is_authenticated=False), False),
        (SimpleNamespace(is_authenticated=1), False),
        (SimpleNamespace(is_authenticated="yes"), False),
        (SimpleNamespace(), False),
        (None, False),
    ],
)
def test_only_true_itself_authenticates(
    caller: object,
    expected: bool,  # noqa: FBT001
) -> None:
    """A truthy `is_authenticated` that is not `True` authenticates nobody."""
    assert is_authenticated(caller) is expected


def test_a_truthy_caller_names_nobody() -> None:
    """The subject follows the same rule as the route check."""
    assert (
        subject_of(SimpleNamespace(is_authenticated=1, subject="user-1"))
        is None
    )
    assert (
        subject_of(SimpleNamespace(is_authenticated=True, subject="user-1"))
        == "user-1"
    )
