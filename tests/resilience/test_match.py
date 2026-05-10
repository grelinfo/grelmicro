"""Match DSL coverage."""

from __future__ import annotations

import re

import pytest

from grelmicro.resilience import Match, Outcome

# --- Match.exception -------------------------------------------------------


def test_exception_single_class() -> None:
    """Single class engages on instance, not on others."""
    f = Match.exception(ValueError)
    assert f(Outcome.from_exception(ValueError("x")))
    assert not f(Outcome.from_exception(KeyError("x")))
    assert not f(Outcome.from_result(42))


def test_exception_subclass_match() -> None:
    """Subclasses of the matched type engage."""

    class CustomError(ValueError):
        pass

    f = Match.exception(ValueError)
    assert f(Outcome.from_exception(CustomError()))


def test_exception_multiple_classes() -> None:
    """Multiple classes engage on any of them."""
    f = Match.exception(ValueError, KeyError)
    assert f(Outcome.from_exception(ValueError()))
    assert f(Outcome.from_exception(KeyError()))
    assert not f(Outcome.from_exception(TypeError()))


def test_exception_predicate() -> None:
    """A callable predicate replaces the class list."""
    f = Match.exception(lambda exc: "foo" in str(exc))
    assert f(Outcome.from_exception(ValueError("foo bar")))
    assert not f(Outcome.from_exception(ValueError("bar")))


def test_exception_requires_arguments() -> None:
    """Empty call raises ``TypeError``."""
    with pytest.raises(TypeError, match="at least one"):
        Match.exception()


def test_exception_rejects_non_exception_class() -> None:
    """Non-exception class raises ``TypeError``."""
    with pytest.raises(TypeError, match="exception classes"):
        Match.exception(int)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- Match.result ----------------------------------------------------------


def test_result_literal() -> None:
    """Literal value matches by equality."""
    f = Match.result(None)
    assert f(Outcome.from_result(None))
    assert not f(Outcome.from_result(0))
    assert not f(Outcome.from_exception(ValueError()))


def test_result_false_literal() -> None:
    """``False`` matches by equality, not truthiness."""
    f = Match.result(False)  # noqa: FBT003
    assert f(Outcome.from_result(False))  # noqa: FBT003
    assert not f(Outcome.from_result(True))  # noqa: FBT003


_THRESHOLD = 100


def test_result_predicate() -> None:
    """Callable is treated as a predicate, even if class-like."""
    f = Match.result(lambda r: r > _THRESHOLD)
    assert f(Outcome.from_result(200))
    assert not f(Outcome.from_result(50))


def test_result_skips_when_raised() -> None:
    """``Match.result`` returns ``False`` for raised outcomes."""
    f = Match.result(None)
    assert not f(Outcome.from_exception(ValueError()))


# --- Match.exception_message -----------------------------------------------


def test_exception_message_contains() -> None:
    """``contains=`` matches a substring of the exception message."""
    f = Match.exception_message(contains="timeout")
    assert f(Outcome.from_exception(RuntimeError("connection timeout")))
    assert not f(Outcome.from_exception(RuntimeError("hello")))


def test_exception_message_regex_string() -> None:
    """``regex=`` accepts a string pattern."""
    f = Match.exception_message(regex=r"\d{3}")
    assert f(Outcome.from_exception(RuntimeError("error 500")))
    assert not f(Outcome.from_exception(RuntimeError("error")))


def test_exception_message_regex_compiled() -> None:
    """``regex=`` accepts a compiled pattern."""
    f = Match.exception_message(regex=re.compile(r"^bad", re.IGNORECASE))
    assert f(Outcome.from_exception(RuntimeError("BAD input")))


def test_exception_message_requires_one_arg() -> None:
    """Both ``contains=`` and ``regex=`` set raises."""
    with pytest.raises(TypeError, match="exactly one"):
        Match.exception_message(contains="x", regex="y")
    with pytest.raises(TypeError, match="exactly one"):
        Match.exception_message()


# --- Match.exception_cause -------------------------------------------------


def test_exception_cause_type() -> None:
    """Match on ``exc.__cause__`` type."""
    inner = ValueError("cause")
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    f = Match.exception_cause(ValueError)
    assert f(Outcome.from_exception(outer))


def test_exception_cause_no_cause() -> None:
    """Exception without a cause does not match."""
    f = Match.exception_cause(ValueError)
    assert not f(Outcome.from_exception(RuntimeError("no cause")))


def test_exception_cause_predicate() -> None:
    """Predicate sees the cause directly."""
    inner = ValueError("oops")
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    f = Match.exception_cause(lambda c: c is not None and "oops" in str(c))
    assert f(Outcome.from_exception(outer))


def test_exception_cause_requires_args() -> None:
    """Empty call raises."""
    with pytest.raises(TypeError, match="at least one"):
        Match.exception_cause()


# --- Match.always / never --------------------------------------------------


def test_always() -> None:
    """``Match.always()`` engages on every outcome."""
    assert Match.always()(Outcome.from_result(42))
    assert Match.always()(Outcome.from_exception(ValueError()))


def test_never() -> None:
    """``Match.never()`` engages on no outcome."""
    assert not Match.never()(Outcome.from_result(42))
    assert not Match.never()(Outcome.from_exception(ValueError()))


# --- Match.predicate -------------------------------------------------------


def test_predicate_full_outcome() -> None:
    """``Match.predicate`` sees the whole outcome."""

    def both_sides(o: Outcome[object]) -> bool:
        return o.raised or o.result is None

    f = Match.predicate(both_sides)
    assert f(Outcome.from_exception(ValueError()))
    assert f(Outcome.from_result(None))
    assert not f(Outcome.from_result(42))


# --- Combinators -----------------------------------------------------------


def test_or_combinator() -> None:
    """``|`` engages when either side engages."""
    f = Match.exception(ValueError) | Match.result(None)
    assert f(Outcome.from_exception(ValueError()))
    assert f(Outcome.from_result(None))
    assert not f(Outcome.from_result(1))


def test_and_combinator() -> None:
    """``&`` engages only when both sides engage."""
    f = Match.exception(ValueError) & Match.exception(lambda e: "x" in str(e))
    assert f(Outcome.from_exception(ValueError("x")))
    assert not f(Outcome.from_exception(ValueError("y")))


# --- Negated forms (`not_*`) ----------------------------------------------


def test_not_exception_scoped_to_raised() -> None:
    """``not_exception`` engages only on raised outcomes whose type does not match."""
    f = Match.not_exception(ValueError)
    assert not f(Outcome.from_exception(ValueError()))
    assert f(Outcome.from_exception(KeyError()))
    # Returned outcomes never engage: the matcher is scoped to raised.
    assert not f(Outcome.from_result(1))


def test_not_result_scoped_to_returned() -> None:
    """``not_result`` engages only on returned outcomes whose value does not match."""
    f = Match.not_result(None)
    assert not f(Outcome.from_result(None))
    assert f(Outcome.from_result(0))
    # Raised outcomes never engage: the matcher is scoped to returned.
    assert not f(Outcome.from_exception(ValueError()))


def test_not_exception_with_predicate_scoped_to_raised() -> None:
    """``not_exception`` with a predicate is also scoped to raised."""
    f = Match.not_exception(lambda exc: "x" in str(exc))
    assert not f(Outcome.from_exception(ValueError("x")))
    assert f(Outcome.from_exception(ValueError("y")))
    assert not f(Outcome.from_result(42))


def test_exception_predicate_skips_when_returned() -> None:
    """A predicate-based ``Match.exception`` returns False on a returned outcome."""
    f = Match.exception(lambda _exc: True)
    assert not f(Outcome.from_result(42))


def test_exception_cause_predicate_skips_when_returned() -> None:
    """``Match.exception_cause(predicate)`` returns False on a returned outcome."""
    f = Match.exception_cause(lambda _cause: True)
    assert not f(Outcome.from_result(42))


def test_exception_cause_skips_when_returned() -> None:
    """``Match.exception_cause`` returns False on a returned outcome."""
    f = Match.exception_cause(KeyError)
    assert not f(Outcome.from_result(42))


def test_exception_cause_rejects_non_exception_class() -> None:
    """Non-exception arg raises ``TypeError``."""
    with pytest.raises(TypeError, match="exception classes"):
        Match.exception_cause(int)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_not_exception_message_scoped_to_raised() -> None:
    """``not_exception_message`` engages only on raised outcomes whose message does not match."""
    f = Match.not_exception_message(contains="timeout")
    assert not f(Outcome.from_exception(RuntimeError("connection timeout")))
    assert f(Outcome.from_exception(RuntimeError("ok")))
    assert not f(Outcome.from_result("anything"))


def test_not_exception_cause_scoped_to_raised() -> None:
    """``not_exception_cause`` engages only on raised outcomes whose cause does not match."""
    inner = ValueError("cause")
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    f = Match.not_exception_cause(ValueError)
    assert not f(Outcome.from_exception(outer))
    assert f(Outcome.from_exception(RuntimeError("no cause")))
    assert not f(Outcome.from_result(42))


# --- Repr ------------------------------------------------------------------


def test_repr_round_trip() -> None:
    """Match objects render as ``Match.<spec>``."""
    f = Match.exception(ValueError) | Match.result(None)
    text = repr(f)
    assert "exception(ValueError)" in text
    assert "result(None)" in text
