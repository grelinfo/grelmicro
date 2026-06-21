"""Exact-value Match tests that pin the negated-form delegation.

The negated builders forward their argument to the positive builder.
A dropped value (``not_result(v)`` building from None) or a dropped
keyword (``not_exception_message(regex=...)`` losing the regex) would
change which outcomes engage, so these tests use a non-None value and
the regex keyword to catch it.
"""

from grelmicro.resilience import Match, Outcome

_VALUE = 5


def test_not_result_forwards_non_none_value() -> None:
    """`not_result(5)` engages on every returned value except exactly 5."""
    f = Match.not_result(_VALUE)
    assert not f(Outcome.from_result(_VALUE))
    assert f(Outcome.from_result(0))
    assert f(Outcome.from_result(None))


def test_not_exception_message_forwards_regex() -> None:
    """`not_exception_message(regex=...)` engages on a non-matching message."""
    f = Match.not_exception_message(regex=r"tim\w+t")
    assert not f(Outcome.from_exception(RuntimeError("connection timeout")))
    assert f(Outcome.from_exception(RuntimeError("ok")))
    assert not f(Outcome.from_result("anything"))


def test_not_exception_message_regex_only_does_not_need_contains() -> None:
    """The regex-only negated form builds without a contains argument."""
    # A dropped regex keyword would leave both arguments None and raise.
    f = Match.not_exception_message(regex=r"^boom$")
    assert f(Outcome.from_exception(RuntimeError("not boom here")))
    assert not f(Outcome.from_exception(RuntimeError("boom")))
