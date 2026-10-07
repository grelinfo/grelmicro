"""`Match` equality, and the classes a class filter names."""

from __future__ import annotations

from typing import Any

import pytest

from grelmicro.resilience import Match, Outcome
from grelmicro.resilience._match import matches_raised_type, named_classes


class _EqualsEverythingMeta(type):
    """A metaclass that answers `==` with True and cannot be hashed."""

    def __eq__(cls, other: object) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]


class _OddError(Exception, metaclass=_EqualsEverythingMeta):
    """An exception class whose metaclass overrides `==` and drops hashing."""


class _OtherOddError(Exception, metaclass=_EqualsEverythingMeta):
    """A second class with the same metaclass."""


class _PlainSubclass(Match):
    """A `Match` subclass that changes nothing."""

    __slots__ = ()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (Match.exception(KeyError), Match.exception(KeyError)),
        (
            Match.exception(KeyError, OSError),
            Match.exception(KeyError, OSError),
        ),
        (Match.not_exception(KeyError), Match.not_exception(KeyError)),
    ],
    ids=["exception", "tuple", "not-exception"],
)
def test_match_class_filters_built_twice_compare_equal(
    first: Match, second: Match
) -> None:
    """Two class filters naming the same classes are equal and hash alike."""
    # Assert
    assert first == second
    assert hash(first) == hash(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (Match.exception(KeyError), Match.exception(OSError)),
        (Match.exception(KeyError), Match.exception(KeyError, OSError)),
        (Match.exception(KeyError), Match.not_exception(KeyError)),
        (Match.exception(KeyError), Match.result(None)),
        (
            Match.exception(lambda _error: True),
            Match.exception(lambda _error: True),
        ),
    ],
    ids=["other-class", "more-classes", "negated", "result", "predicates"],
)
def test_match_different_filters_compare_unequal(
    first: Match, second: Match
) -> None:
    """Filters that may answer differently are not equal."""
    # Assert
    assert first != second


def test_match_subclass_filter_is_not_equal_to_a_plain_filter() -> None:
    """A filter built by a `Match` subclass never equals a plain one."""
    # Arrange
    plain = Match.exception(KeyError)

    # Act
    subclassed = _PlainSubclass.exception(KeyError)

    # Assert
    assert subclassed != plain
    assert plain != subclassed


def test_match_compared_with_other_object_is_not_equal() -> None:
    """A `Match` is never equal to an object of another type."""
    # Assert
    assert Match.exception(KeyError) != "Match.exception(KeyError)"


def test_match_predicate_filter_equals_itself() -> None:
    """A predicate filter is equal to itself and hashes stably."""
    # Arrange
    match = Match.result(None)

    # Assert
    assert match == match  # noqa: PLR0124
    assert hash(match) == hash(match)


def test_match_metaclass_equality_never_runs() -> None:
    """A class whose metaclass overrides `==` and hashing works in a filter."""
    # Arrange
    odd = Match.exception(_OddError)
    other = Match.exception(_OtherOddError)

    # Act
    joined = odd | other

    # Assert
    assert odd != other
    assert odd == Match.exception(_OddError)
    assert hash(odd) == hash(Match.exception(_OddError))
    assert named_classes(joined) == (_OddError, _OtherOddError)


def test_match_union_lists_each_class_once() -> None:
    """Joining filters that share a class lists it once."""
    # Arrange
    joined = Match.exception(KeyError, ValueError) | Match.exception(ValueError)

    # Act
    same = Match.exception(KeyError, ValueError)

    # Assert
    assert joined == same


def test_match_union_leaves_its_operands_unchanged() -> None:
    """Joining two filters builds a new one and changes neither operand."""
    # Arrange
    left = Match.exception(KeyError)
    right = Match.exception(ValueError)

    # Act
    left | right

    # Assert
    assert left == Match.exception(KeyError)
    assert named_classes(left) == (KeyError,)


def test_match_union_with_a_subclass_names_no_classes() -> None:
    """Joining with a `Match` subclass keeps no class list, only its answer."""

    # Arrange
    class _Always(Match):
        __slots__ = ()

        def __call__(self, outcome: Outcome[Any]) -> bool:  # noqa: ARG002
            return True

    # Act
    joined = Match.exception(KeyError) | _Always.exception(KeyError)

    # Assert
    assert named_classes(joined) is None
    assert joined(Outcome.from_exception(ValueError())) is True


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        (Match.exception(KeyError, ValueError), (KeyError, ValueError)),
        (Match.not_exception(KeyError), None),
        (Match.exception(lambda _error: True), None),
        (Match.result(None), None),
        (_PlainSubclass.exception(KeyError), None),
    ],
    ids=["classes", "negated", "predicate", "result", "match-subclass"],
)
def test_named_classes_answers_for_plain_class_filters_only(
    match: Match, expected: tuple[type[Exception], ...] | None
) -> None:
    """Only a filter engaging on its own classes names them."""
    # Assert
    assert named_classes(match) == expected


@pytest.mark.parametrize(
    ("match", "exception_type", "expected"),
    [
        (Match.exception(LookupError), KeyError, True),
        (Match.exception(LookupError), ValueError, False),
        (Match.not_exception(LookupError), ValueError, True),
        (Match.exception(lambda _error: True), ValueError, None),
        (_PlainSubclass.exception(KeyError), KeyError, None),
        (Match.exception(KeyError), "not-a-class", None),
    ],
    ids=[
        "subclass",
        "other",
        "negated",
        "predicate",
        "match-subclass",
        "not-a-class",
    ],
)
def test_matches_raised_type_answers_for_class_filters_only(
    match: Match,
    exception_type: Any,  # noqa: ANN401
    expected: bool | None,  # noqa: FBT001
) -> None:
    """A plain class filter answers from the type, any other gives `None`."""
    # Act
    matched = matches_raised_type(match, exception_type)

    # Assert
    assert matched is expected


def test_matches_raised_type_interrupt_propagates() -> None:
    """An interrupt while checking the type propagates."""

    # Arrange
    class _InterruptingBases:
        """A class-like object whose bases are interrupted."""

        @property
        def __bases__(self) -> tuple[type, ...]:
            raise KeyboardInterrupt

    # Act / Assert
    with pytest.raises(KeyboardInterrupt):
        matches_raised_type(
            Match.exception(KeyError),
            _InterruptingBases(),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )
