"""Match DSL coverage."""

from __future__ import annotations

import gc
import logging
import operator
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from grelmicro.resilience import Match, Outcome
from grelmicro.resilience._match import (
    _already_warned,
    _coerce_bool,
    _describe,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# --- Match.exception -------------------------------------------------------


_REPEAT_CALLS = 5
_HAMMER_THREADS = 8
_HAMMER_ROUNDS = 200
_OVER_THE_BOUND = 5


class _HostileRepr:
    """A predicate that cannot be weakly held and refuses to be repr'd.

    `__slots__` without `__weakref__` blocks the weak reference, so it
    takes the untrackable path, and then naming it for the warning runs
    the `__repr__` that raises.
    """

    __slots__ = ()

    def __call__(self, _exc: Exception) -> int:
        """Return a non-bool, to trigger the warning."""
        return 1

    def __repr__(self) -> str:
        """Raise, as a badly behaved object can."""
        msg = "repr exploded"
        raise RuntimeError(msg)


class _BoolBomb:
    """A predicate return value whose truth value raises."""

    def __bool__(self) -> bool:
        """Raise, as a badly behaved value can."""
        msg = "truth value exploded"
        raise RuntimeError(msg)


class _UnnameableMeta(type):
    """A metaclass that raises from `__name__`, blocking the last resort."""

    @property
    def __name__(cls) -> str:
        """Raise, so `type(predicate).__name__` cannot be read either."""
        msg = "metaclass __name__"
        raise RuntimeError(msg)


class _Unnameable(metaclass=_UnnameableMeta):
    """A predicate that refuses every way of naming it."""

    def __call__(self, _exc: Exception) -> int:
        """Return a non-bool, to trigger the warning."""
        return 1

    def __getattr__(self, name: str) -> object:
        """Raise, so `__name__` cannot be read."""
        msg = "getattr"
        raise RuntimeError(msg)

    def __repr__(self) -> str:
        """Raise, so the repr fallback cannot be used."""
        msg = "repr"
        raise RuntimeError(msg)


class _RaisingHash:
    """A predicate whose `__hash__` raises, so the lookup itself fails."""

    def __call__(self, _exc: Exception) -> int:
        """Return a non-bool, to trigger the warning."""
        return 1

    def __hash__(self) -> int:
        """Raise, as a badly behaved object can."""
        msg = "no hash for you"
        raise ValueError(msg)


class _RaisingGetattr:
    """A predicate whose attribute lookup raises, like an unbound proxy."""

    def __call__(self, _exc: Exception) -> int:
        """Return a non-bool, to trigger the warning."""
        return 1

    def __getattr__(self, name: str) -> object:
        """Raise, as a proxy outside its context does."""
        msg = "working outside of application context"
        raise RuntimeError(msg)


@dataclass
class _UnhashablePredicate:
    """A predicate a `WeakSet` cannot look up, because it is unhashable.

    Weak-referenceable like any ordinary instance. `@dataclass` sets
    `__hash__` to None once it defines `__eq__`, and that alone is enough
    to make the membership test raise.
    """

    n: int = 1

    def __call__(self, _exc: Exception) -> int:
        """Return a non-bool, to trigger the warning."""
        return 1


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
    """Empty call raises ``ValueError``."""
    with pytest.raises(ValueError, match="at least one"):
        Match.exception()


def test_exception_rejects_non_exception_class() -> None:
    """Non-exception class raises ``ValueError``."""
    with pytest.raises(ValueError, match="exception classes"):
        Match.exception(int)  # ty: ignore[invalid-argument-type]


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
    with pytest.raises(ValueError, match="at least one"):
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
    with pytest.raises(ValueError, match="exception classes"):
        Match.exception_cause(int)  # ty: ignore[invalid-argument-type]


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


# --- explain() -------------------------------------------------------------


def test_explain_returns_repr() -> None:
    """``explain()`` returns the same string as ``repr()``."""
    m = Match.exception(ValueError)
    assert m.explain() == repr(m)


def test_explain_on_composite() -> None:
    """``explain()`` includes both branches for a composed matcher."""
    m = Match.exception(ValueError) | Match.result(None)
    text = m.explain()
    assert "exception(ValueError)" in text
    assert "result(None)" in text


# --- Non-bool predicate coercion -------------------------------------------


def test_predicate_non_bool_coerces_truthy() -> None:
    """A predicate returning a truthy non-bool value still engages the match."""
    f = Match.exception(lambda _exc: 1)  # ty: ignore[invalid-argument-type]
    assert f(Outcome.from_exception(ValueError("x")))


def test_predicate_non_bool_coerces_falsy() -> None:
    """A predicate returning a falsy non-bool value does not engage the match."""
    f = Match.exception(lambda _exc: 0)  # ty: ignore[invalid-argument-type]
    assert not f(Outcome.from_exception(ValueError("x")))


def test_predicate_non_bool_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A predicate returning a non-bool value logs a warning on first call."""
    import grelmicro.resilience._match as match_mod  # noqa: PLC0415

    def truthy_int(_exc: Exception) -> int:
        return 1

    match_mod._warned_predicates.discard(truthy_int)

    f = Match.exception(truthy_int)  # ty: ignore[invalid-argument-type]
    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        f(Outcome.from_exception(ValueError("x")))

    assert any("non-bool" in record.message for record in caplog.records)


def test_predicate_non_bool_warns_only_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The non-bool warning fires at most once per predicate."""
    import grelmicro.resilience._match as match_mod  # noqa: PLC0415

    def truthy_int(_exc: Exception) -> int:
        return 1

    match_mod._warned_predicates.discard(truthy_int)

    f = Match.exception(truthy_int)  # ty: ignore[invalid-argument-type]
    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        f(Outcome.from_exception(ValueError("x")))
        f(Outcome.from_exception(ValueError("y")))

    warning_count = sum(
        1 for record in caplog.records if "non-bool" in record.message
    )
    assert warning_count == 1


def test_result_predicate_non_bool_coerces() -> None:
    """``Match.result`` with a non-bool predicate still coerces correctly."""
    f = Match.result(lambda r: 1 if r > 0 else 0)
    assert f(Outcome.from_result(5))
    assert not f(Outcome.from_result(-1))


def test_exception_cause_predicate_non_bool_coerces() -> None:
    """``Match.exception_cause`` with a non-bool predicate still coerces."""
    inner = ValueError("cause")
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    f = Match.exception_cause(lambda c: 1 if c is not None else 0)  # ty: ignore[invalid-argument-type]
    assert f(Outcome.from_exception(outer))


# --- Repr ------------------------------------------------------------------


def test_repr_round_trip() -> None:
    """Match objects render as ``Match.<spec>``."""
    f = Match.exception(ValueError) | Match.result(None)
    text = repr(f)
    assert "exception(ValueError)" in text
    assert "result(None)" in text


def _truthy_predicate() -> object:
    """Return a fresh predicate that returns a non-bool."""

    def truthy(_exc: Exception) -> int:
        return 1

    return truthy


def test_the_warning_registry_forgets_a_collected_predicate() -> None:
    """A predicate that is gone must not stay remembered.

    The registry held `id(predicate)` and nothing else, so an entry
    outlived its predicate. CPython then handed that address to the next
    one, whose warning was silently suppressed, and the caller never
    learned their predicate returns a non-bool. The entries also piled up
    for every predicate ever built.
    """
    import grelmicro.resilience._match as match_mod  # noqa: PLC0415

    outcome = Outcome.from_exception(ValueError("x"))
    gc.collect()
    before = len(match_mod._warned_predicates)

    predicate = _truthy_predicate()
    Match.exception(predicate)(outcome)  # ty: ignore[invalid-argument-type]
    assert len(match_mod._warned_predicates) == before + 1

    del predicate
    gc.collect()

    assert len(match_mod._warned_predicates) == before


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: operator.attrgetter("errno"), id="not-weak-referenceable"
        ),
        pytest.param(_UnhashablePredicate, id="unhashable"),
        pytest.param(_RaisingHash, id="raising-hash"),
        pytest.param(_RaisingGetattr, id="raising-getattr"),
    ],
)
def test_a_predicate_that_cannot_be_tracked_warns_instead_of_raising(
    factory: Callable[[], object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tracking a predicate must never raise out of the matcher.

    A matcher runs inside the resilience machinery, and `Retry` calls it
    from an `except` block, so an exception raised here would replace the
    error the caller was already handling. Three of these refuse to be
    tracked, each a different way: no weak reference, no hash, a
    `__hash__` that raises. The fourth tracks fine and refuses to be
    named, because its attribute lookup raises.

    Built per call, so a repeated run does not find the predicate already
    registered from the previous pass.
    """
    predicate = factory()
    outcome = Outcome.from_exception(OSError(2, "boom"))

    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        result = Match.exception(predicate)(outcome)  # ty: ignore[invalid-argument-type]

    assert result is True
    assert any("non-bool" in record.message for record in caplog.records)


def test_an_untrackable_predicate_is_still_warned_only_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A predicate a `WeakSet` cannot hold must not warn on every attempt.

    A `Retry` calls the matcher once per attempt, so warning per call
    would flood the log of a service whose predicate returns a non-bool.
    """
    predicate = operator.attrgetter("args")
    outcome = Outcome.from_exception(ValueError("x"))
    matcher = Match.exception(predicate)

    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        for _ in range(_REPEAT_CALLS):
            matcher(outcome)

    warnings = [r for r in caplog.records if "non-bool" in r.message]
    assert len(warnings) == 1


def test_a_predicate_whose_repr_raises_does_not_break_the_matcher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Naming a predicate must never raise out of the matcher.

    `Retry` calls the matcher from an `except` block, so an error raised
    while building the warning would replace the one the caller was
    already handling.
    """
    outcome = Outcome.from_exception(ValueError("x"))

    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        result = Match.exception(_HostileRepr())(outcome)  # ty: ignore[invalid-argument-type]

    assert result is True
    assert any("non-bool" in record.message for record in caplog.records)


def test_the_untrackable_registry_stops_growing_at_its_bound() -> None:
    """Past the bound, an untrackable predicate reports again rather than growing.

    Each entry keeps its predicate alive, which is what makes the address
    safe to key on, so the registry has to stop somewhere.
    """
    import grelmicro.resilience._match as match_mod  # noqa: PLC0415

    saved = dict(match_mod._warned_untrackable)
    outcome = Outcome.from_exception(ValueError("x"))
    # Held in a list so every address stays distinct.
    predicates = [
        operator.attrgetter("args")
        for _ in range(match_mod._UNTRACKABLE_LIMIT + _OVER_THE_BOUND)
    ]

    try:
        match_mod._warned_untrackable.clear()
        for predicate in predicates:
            Match.exception(predicate)(outcome)

        assert (
            len(match_mod._warned_untrackable) == match_mod._UNTRACKABLE_LIMIT
        )
    finally:
        match_mod._warned_untrackable.clear()
        match_mod._warned_untrackable.update(saved)


def test_a_result_whose_truth_value_raises_reads_as_no_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Coercing the predicate's return must not raise out of the matcher.

    Coercion is the whole job of `_coerce_bool`, and `Retry` calls the
    matcher from an `except` block, so raising here would replace the
    error the caller is handling with one from the matcher itself.
    """
    outcome = Outcome.from_exception(ValueError("original"))

    with caplog.at_level(logging.WARNING, logger="grelmicro.resilience"):
        matched = Match.exception(lambda _exc: _BoolBomb())(outcome)  # ty: ignore[invalid-argument-type]

    assert matched is False
    assert any("no match" in record.message for record in caplog.records)


def test_a_predicate_that_refuses_every_name_still_gets_one() -> None:
    """Naming falls back past `__getattr__`, `__repr__` and the metaclass.

    The last resort reads `type(predicate).__name__`, which a metaclass
    property can hijack and raise from, so even that is guarded.
    """
    assert _describe(_Unnameable()) == "<predicate>"


class _ClassBomb:
    """A lazy-proxy shape: `__class__` raises while the proxy is unbound."""

    def __call__(self, _exc: Exception) -> bool:
        """Match everything."""
        return True

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Raise, as an unbound proxy does."""
        msg = "__class__ property"
        raise RuntimeError(msg)


class _ClassLiar:
    """An object whose `__class__` claims to be `type`, reaching issubclass."""

    def __call__(self, _exc: Exception) -> bool:
        """Match everything."""
        return True

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Report `type`, so the class check passes and `issubclass` runs."""
        return type


class _RaisingStrError(Exception):
    """An exception that raises while being rendered, like a lazy driver error."""

    def __str__(self) -> str:
        """Raise, as a message built from a closed connection can."""
        msg = "exception __str__"
        raise RuntimeError(msg)


class _RaisingEq:
    """A value that raises while being compared."""

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, _other: object) -> bool:
        """Raise, as caller code can."""
        msg = "result __eq__"
        raise RuntimeError(msg)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: Match.exception(_ClassBomb()), id="exception-proxy"
        ),
        pytest.param(lambda: Match.result(_ClassBomb()), id="result-proxy"),
        pytest.param(
            lambda: Match.exception_cause(_ClassBomb()),  # ty: ignore[invalid-argument-type]
            id="cause-proxy",
        ),
        pytest.param(lambda: Match.result(_HostileRepr()), id="literal-repr"),
        pytest.param(
            lambda: Match.not_result(_HostileRepr()), id="not-literal-repr"
        ),
    ],
)
def test_building_a_match_survives_a_hostile_argument(
    build: Callable[[], Match],
) -> None:
    """Classifying and naming an argument must not raise an arbitrary error.

    A lazy proxy forwards `__class__` to an object that raises while
    unbound, and `isinstance` reads `__class__`, so the classification
    branch used to propagate whatever the proxy raised.
    """
    assert build() is not None


def test_an_argument_posing_as_a_class_is_refused_with_a_value_error() -> None:
    """An object claiming to be `type` gets the documented argument error.

    It slips past the class check and reaches `issubclass`, which raised
    `TypeError`. Pydantic converts only `ValueError`, so that escaped
    every documented `except`.
    """
    with pytest.raises(ValueError, match="must all be exception classes"):
        Match.exception(_ClassLiar())


@pytest.mark.parametrize(
    ("matcher", "outcome"),
    [
        pytest.param(
            Match.exception_message(contains="x"),
            Outcome.from_exception(_RaisingStrError()),
            id="message-contains",
        ),
        pytest.param(
            Match.exception_message(regex="x"),
            Outcome.from_exception(_RaisingStrError()),
            id="message-regex",
        ),
        pytest.param(
            Match.result(0),
            Outcome.from_result(_RaisingEq()),
            id="literal-equality",
        ),
    ],
)
def test_matching_survives_a_hostile_outcome(
    matcher: Match, outcome: Outcome[object]
) -> None:
    """Reading a message or comparing a result must not raise.

    `Retry` calls the matcher from an `except` block, so anything raised
    here replaces the error the caller is handling.
    """
    result = matcher(outcome)

    assert result is False


class _BaseBoom(BaseException):
    """A `BaseException`, which `except Exception` would let through."""


class _ResultMeta(type):
    """A metaclass that raises from `__name__`, so naming the result fails."""

    @property
    def __name__(cls) -> str:
        """Raise, blocking the last resort used to name a result."""
        msg = "metaclass __name__"
        raise RuntimeError(msg)


class _UndecidableResult(metaclass=_ResultMeta):
    """A predicate return whose truth value and whose type name both raise."""

    def __bool__(self) -> bool:
        """Raise, as a badly behaved value can."""
        msg = "truthiness undecidable"
        raise RuntimeError(msg)


class _ReprRecurses:
    """A predicate return whose `repr` recurses until the stack gives out."""

    def __repr__(self) -> str:
        """Recurse, which logging re-raises rather than swallowing."""
        return repr(self)


class _BoolBaseException:
    """A predicate return whose truth value raises a `BaseException`."""

    def __bool__(self) -> bool:
        """Raise below `Exception`, which a narrow guard would miss."""
        raise _BaseBoom


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(
            _UndecidableResult(), False, id="unnameable-and-undecidable"
        ),
        # Truthy on its own terms: only naming it for the warning fails.
        pytest.param(_ReprRecurses(), True, id="repr-recurses"),
        pytest.param(
            _BoolBaseException(), False, id="bool-raises-base-exception"
        ),
    ],
)
def test_coercing_a_hostile_result_never_raises(
    result: object,
    *,
    expected: bool,
) -> None:
    """No predicate return may raise out of the matcher.

    `Retry` calls the matcher from an `except` block, so anything raised
    replaces the error the caller is handling. Naming the result, writing
    the warning, and coercing the value are all caller-controlled code. A
    value whose truth cannot be read counts as no match.
    """
    assert _coerce_bool(result, len) is expected


def test_bookkeeping_survives_a_base_exception_from_hashing() -> None:
    """A predicate that raises below `Exception` while being tracked is fine."""

    class HashBoom:
        def __hash__(self) -> int:
            raise _BaseBoom

    assert _already_warned(HashBoom()) is False


def test_the_untrackable_registry_survives_concurrent_eviction() -> None:
    """Threads racing the same eviction must not escape the matcher.

    The eviction reads the oldest key and pops it in separate steps, so
    two threads can select the same one, and a third can resize the
    registry mid-iteration.
    """

    class SlotPredicate:
        __slots__ = ("threshold",)

        def __init__(self, threshold: int) -> None:
            self.threshold = threshold

        def __call__(self, result: object) -> object:
            return result

    escapes: list[str] = []

    def hammer(base: int) -> None:
        for index in range(_HAMMER_ROUNDS):
            try:
                Match.result(SlotPredicate(base + index))(
                    Outcome.from_result(1)
                )
            except BaseException as exc:  # noqa: BLE001
                escapes.append(type(exc).__name__)

    threads = [
        threading.Thread(target=hammer, args=(worker * 10_000,))
        for worker in range(_HAMMER_THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert escapes == []
