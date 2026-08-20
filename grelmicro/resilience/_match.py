"""Outcome filter DSL for resilience strategies.

The [`Match`][grelmicro.resilience.Match] class is the building
block every resilience strategy uses to decide whether an
[`Outcome`][grelmicro.resilience.Outcome] should engage the
strategy. Match instances compose with ``|`` (or) and ``&`` (and).
Each primitive matcher has a symmetric ``not_*`` twin for the
negated form.

Example:
```python
from grelmicro.resilience import Match, Retry

policy = Retry(
    "payments",
    when=Match.exception(httpx.HTTPError) | Match.result(None),
    attempts=3,
)
```
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, cast
from weakref import WeakSet

from grelmicro.resilience._outcome import Outcome

_log = logging.getLogger("grelmicro.resilience")
_warned_predicates: WeakSet[Any] = WeakSet()
"""Predicates already warned about, dropped when the predicate is."""

_warned_untrackable: dict[int, Any] = {}
"""Predicates a `WeakSet` cannot hold, kept by address and by reference.

The reference is what makes the address usable as a key: the predicate
cannot be collected, so nothing else is handed its address. Oldest first
out, so a program minting these endlessly stays bounded.
"""

_UNTRACKABLE_LIMIT = 128
"""How many untrackable predicates are remembered before the oldest is dropped."""


def _describe(predicate: Any) -> str:  # noqa: ANN401
    """Return a readable name for a predicate, whatever it is.

    Every step is guarded: a lazy proxy can raise from `__getattr__`, a
    `__name__` property can raise, and `__repr__` can raise.
    """
    try:
        name = getattr(predicate, "__name__", None)
        if name is not None:
            return str(name)
        return repr(predicate)
    except Exception:  # noqa: BLE001
        return type(predicate).__name__


def _already_warned(predicate: Any) -> bool:  # noqa: ANN401
    """Return whether this predicate was warned about, recording it if not.

    Held weakly, so a collected predicate stops speaking for the next one
    allocated at its address. One that cannot be held that way is kept by
    address alongside a reference, and the oldest is dropped once
    `_UNTRACKABLE_LIMIT` is reached, so the warning stays once per
    predicate without the registry growing.

    Nothing here raises. A matcher runs inside `Retry`'s `except` block,
    where an error would replace the one the caller is handling, and a
    predicate is free to raise from `__hash__` or `__eq__`.
    """
    try:
        if predicate in _warned_predicates:
            return True
        _warned_predicates.add(predicate)
    except Exception:  # noqa: BLE001
        key = id(predicate)
        if key in _warned_untrackable:
            return True
        if len(_warned_untrackable) >= _UNTRACKABLE_LIMIT:
            # Key and referent go together, so the address cannot be
            # handed to another predicate while it is still remembered.
            _warned_untrackable.pop(next(iter(_warned_untrackable)))
        _warned_untrackable[key] = predicate
    return False


def _coerce_bool(result: Any, predicate: Any) -> bool:  # noqa: ANN401
    """Return ``bool(result)``, warning if ``result`` was not already ``bool``.

    The warning fires once per predicate, so a tight retry loop reports it
    once rather than on every attempt.
    """
    if type(result) is not bool and not _already_warned(predicate):
        _log.warning(
            "Match predicate %s returned non-bool %r; coercing to bool. "
            "Return an explicit bool to suppress this warning.",
            _describe(predicate),
            result,
        )
    return bool(result)


Matcher = Callable[[Outcome[Any]], bool]
"""Callable signature every Match resolves to.

Returns ``True`` when the outcome should engage the strategy.
"""


class Match:
    """Outcome filter that resilience strategies consume.

    Build instances through the classmethods, never the constructor.
    Compose with the ``|`` and ``&`` operators. Each primitive
    matcher has a symmetric ``not_*`` twin for the negated form.

    Read more in the [Retry filtering](../resilience/retry.md#filtering-outcomes-with-match) docs.
    """

    __slots__ = ("_matcher", "_repr")

    def __init__(self, matcher: Matcher, repr_: str) -> None:
        self._matcher = matcher
        self._repr = repr_

    def __call__(self, outcome: Outcome[Any]) -> bool:
        """Test the outcome against this filter."""
        return self._matcher(outcome)

    def __repr__(self) -> str:
        return f"Match.{self._repr}"

    def explain(self) -> str:
        """Return the human-readable matcher tree for debugging."""
        return repr(self)

    def __or__(self, other: Match) -> Match:
        """Return a Match that engages when either side engages."""
        return Match(
            lambda outcome: self(outcome) or other(outcome),
            f"any({self._repr}, {other._repr})",
        )

    def __and__(self, other: Match) -> Match:
        """Return a Match that engages when both sides engage."""
        return Match(
            lambda outcome: self(outcome) and other(outcome),
            f"all({self._repr}, {other._repr})",
        )

    @classmethod
    def exception(
        cls,
        *exception_types_or_predicate: type[Exception]
        | Callable[[Exception], bool],
    ) -> Match:
        """Engage when the call raised a matching exception.

        Pass one or more exception classes, or a single callable
        predicate ``(Exception) -> bool``. When mixed forms are
        passed (some classes, some callables), the result raises
        ``ValueError`` at construction.
        """
        if not exception_types_or_predicate:
            msg = "Match.exception() requires at least one argument"
            # Reachable from configuration: an empty `GREL_*_WHEN` parses to
            # an empty list. `ValueError` so pydantic wraps it.
            raise ValueError(msg)

        # Single callable that is not a class: predicate path.
        if (
            len(exception_types_or_predicate) == 1
            and callable(exception_types_or_predicate[0])
            and not isinstance(exception_types_or_predicate[0], type)
        ):
            predicate = exception_types_or_predicate[0]

            def _check_predicate(outcome: Outcome[Any]) -> bool:
                exc = outcome.exception
                if not outcome.raised or exc is None:
                    return False
                return _coerce_bool(predicate(exc), predicate)

            return cls(
                _check_predicate,
                f"exception({_describe(predicate)})",
            )

        # All arguments must be exception classes.
        for type_ in exception_types_or_predicate:
            if not (isinstance(type_, type) and issubclass(type_, Exception)):
                msg = (
                    f"Match.exception() arguments must all be exception "
                    f"classes, got {getattr(type_, '__name__', type(type_).__name__)}"
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`, so a `TypeError` raised
                # inside a validator escapes every documented `except`, and
                # escapes `reconfigure_all` too.
                raise ValueError(msg)  # noqa: TRY004
        types = cast(
            "tuple[type[Exception], ...]", tuple(exception_types_or_predicate)
        )

        def _check_types(outcome: Outcome[Any]) -> bool:
            return outcome.raised and isinstance(outcome.exception, types)

        names = ", ".join(t.__name__ for t in types)
        return cls(_check_types, f"exception({names})")

    @classmethod
    def result(
        cls,
        value_or_predicate: Any | Callable[[Any], bool],  # noqa: ANN401
    ) -> Match:
        """Engage when the call returned a matching value.

        Pass a literal value (compared with ``==``) or a callable
        predicate ``(result) -> bool``. Functions are always treated
        as predicates: to match a function literal, wrap it in a
        predicate (``lambda r: r is my_fn``).
        """
        if callable(value_or_predicate) and not isinstance(
            value_or_predicate, type
        ):
            predicate = value_or_predicate

            def _check_predicate(outcome: Outcome[Any]) -> bool:
                return not outcome.raised and _coerce_bool(
                    predicate(outcome.result), predicate
                )

            return cls(
                _check_predicate,
                f"result({_describe(predicate)})",
            )

        value = value_or_predicate

        def _check_value(outcome: Outcome[Any]) -> bool:
            return not outcome.raised and outcome.result == value

        return cls(_check_value, f"result({value!r})")

    @classmethod
    def exception_message(
        cls,
        *,
        contains: str | None = None,
        regex: str | re.Pattern[str] | None = None,
    ) -> Match:
        """Engage when the exception's message matches the predicate.

        Pass exactly one of ``contains=`` (substring) or ``regex=``
        (compiled or string regex).
        """
        if (contains is None) == (regex is None):
            msg = (
                "Match.exception_message() needs exactly one of "
                "contains= or regex="
            )
            raise TypeError(msg)

        if contains is not None:
            needle = contains

            def _check_contains(outcome: Outcome[Any]) -> bool:
                return outcome.raised and needle in str(outcome.exception)

            return cls(
                _check_contains, f"exception_message(contains={contains!r})"
            )

        # ``regex`` is non-None on this branch (mutual-exclusion check above).
        pattern = re.compile(regex) if isinstance(regex, str) else regex
        assert pattern is not None  # noqa: S101

        def _check_regex(outcome: Outcome[Any]) -> bool:
            return (
                outcome.raised
                and pattern.search(str(outcome.exception)) is not None
            )

        return cls(
            _check_regex, f"exception_message(regex={pattern.pattern!r})"
        )

    @classmethod
    def exception_cause(
        cls,
        *exception_types_or_predicate: type[Exception]
        | Callable[[BaseException | None], bool],
    ) -> Match:
        """Engage when the exception's ``__cause__`` matches.

        Same shorthand as ``Match.exception``: one or more classes,
        or a single callable predicate on ``exc.__cause__``.
        """
        if not exception_types_or_predicate:
            msg = "Match.exception_cause() requires at least one argument"
            # Reachable from configuration: an empty `GREL_*_WHEN` parses to
            # an empty list. `ValueError` so pydantic wraps it.
            raise ValueError(msg)

        if (
            len(exception_types_or_predicate) == 1
            and callable(exception_types_or_predicate[0])
            and not isinstance(exception_types_or_predicate[0], type)
        ):
            predicate = exception_types_or_predicate[0]

            def _check_predicate(outcome: Outcome[Any]) -> bool:
                exc = outcome.exception
                if not outcome.raised or exc is None:
                    return False
                return _coerce_bool(predicate(exc.__cause__), predicate)

            return cls(
                _check_predicate,
                f"exception_cause({_describe(predicate)})",
            )

        for type_ in exception_types_or_predicate:
            if not (
                isinstance(type_, type) and issubclass(type_, BaseException)
            ):
                msg = (
                    f"Match.exception_cause() arguments must all be exception "
                    f"classes, got {getattr(type_, '__name__', type(type_).__name__)}"
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`, so a `TypeError` raised
                # inside a validator escapes every documented `except`, and
                # escapes `reconfigure_all` too.
                raise ValueError(msg)  # noqa: TRY004
        types = cast(
            "tuple[type[BaseException], ...]",
            tuple(exception_types_or_predicate),
        )

        def _check_types(outcome: Outcome[Any]) -> bool:
            exc = outcome.exception
            if not outcome.raised or exc is None:
                return False
            return isinstance(exc.__cause__, types)

        names = ", ".join(t.__name__ for t in types)
        return cls(_check_types, f"exception_cause({names})")

    @classmethod
    def always(cls) -> Match:
        """Engage on every outcome.

        Useful as the explicit "always retry" policy.
        Note: ``BaseException`` subclasses outside ``Exception`` are
        still never retried by the strategy itself, regardless of
        the matcher.
        """
        return cls(lambda _outcome: True, "always()")

    @classmethod
    def never(cls) -> Match:
        """Engage on no outcome. Effectively disables the strategy."""
        return cls(lambda _outcome: False, "never()")

    @classmethod
    def predicate(cls, fn: Callable[[Outcome[Any]], bool]) -> Match:
        """Engage when the predicate returns true for the outcome.

        Use this when the filter must observe both the exception and
        the result together. Most call sites should reach for
        ``Match.exception`` or ``Match.result`` instead.
        """
        return cls(
            fn,
            f"predicate({_describe(fn)})",
        )

    # --- Negated forms (symmetric `not_*` prefix) ----------------------
    #
    # Each ``not_*`` matcher is **scoped to the same outcome shape** as
    # its positive twin. ``Match.not_exception(X)`` engages on a raised
    # outcome whose exception is NOT X, and never on a returned
    # outcome. ``Match.not_result(v)`` engages on a returned outcome
    # whose value is NOT v, and never on a raised outcome. This keeps
    # the negated forms safe to use as the sole ``when=`` filter.

    @classmethod
    def not_exception(
        cls,
        *exception_types_or_predicate: type[Exception]
        | Callable[[Exception], bool],
    ) -> Match:
        """Engage when the call raised an exception that does NOT match.

        Scoped to raised outcomes: returns ``False`` for any returned
        outcome. Same arguments as ``Match.exception``.
        """
        positive = cls.exception(*exception_types_or_predicate)

        def _check(outcome: Outcome[Any]) -> bool:
            return outcome.raised and not positive(outcome)

        return cls(
            _check,
            positive._repr.replace("exception(", "not_exception(", 1),  # noqa: SLF001
        )

    @classmethod
    def not_result(
        cls,
        value_or_predicate: Any | Callable[[Any], bool],  # noqa: ANN401
    ) -> Match:
        """Engage when the call returned a value that does NOT match.

        Scoped to returned outcomes: returns ``False`` for any raised
        outcome. Same argument as ``Match.result``.
        """
        positive = cls.result(value_or_predicate)

        def _check(outcome: Outcome[Any]) -> bool:
            return not outcome.raised and not positive(outcome)

        return cls(
            _check,
            positive._repr.replace("result(", "not_result(", 1),  # noqa: SLF001
        )

    @classmethod
    def not_exception_message(
        cls,
        *,
        contains: str | None = None,
        regex: str | re.Pattern[str] | None = None,
    ) -> Match:
        """Engage when the exception's message does NOT match.

        Scoped to raised outcomes: returns ``False`` for any returned
        outcome.
        """
        positive = cls.exception_message(contains=contains, regex=regex)

        def _check(outcome: Outcome[Any]) -> bool:
            return outcome.raised and not positive(outcome)

        return cls(
            _check,
            positive._repr.replace(  # noqa: SLF001
                "exception_message(", "not_exception_message(", 1
            ),
        )

    @classmethod
    def not_exception_cause(
        cls,
        *exception_types_or_predicate: type[Exception]
        | Callable[[BaseException | None], bool],
    ) -> Match:
        """Engage when the exception's ``__cause__`` does NOT match.

        Scoped to raised outcomes: returns ``False`` for any returned
        outcome.
        """
        positive = cls.exception_cause(*exception_types_or_predicate)

        def _check(outcome: Outcome[Any]) -> bool:
            return outcome.raised and not positive(outcome)

        return cls(
            _check,
            positive._repr.replace(  # noqa: SLF001
                "exception_cause(", "not_exception_cause(", 1
            ),
        )
