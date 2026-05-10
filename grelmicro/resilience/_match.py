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

import re
from collections.abc import Callable
from typing import Any, cast

from grelmicro.resilience._outcome import Outcome

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
        ``TypeError`` at construction.
        """
        if not exception_types_or_predicate:
            msg = "Match.exception() requires at least one argument"
            raise TypeError(msg)

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
                return bool(predicate(exc))  # type: ignore[arg-type]

            return cls(
                _check_predicate,
                f"exception({getattr(predicate, '__name__', repr(predicate))})",
            )

        # All arguments must be exception classes.
        for type_ in exception_types_or_predicate:
            if not (isinstance(type_, type) and issubclass(type_, Exception)):
                msg = (
                    "Match.exception() arguments must all be exception "
                    f"classes, got {type_!r}"
                )
                raise TypeError(msg)
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
                return not outcome.raised and bool(predicate(outcome.result))

            return cls(
                _check_predicate,
                f"result({getattr(predicate, '__name__', repr(predicate))})",
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
            raise TypeError(msg)

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
                return bool(predicate(exc.__cause__))  # type: ignore[arg-type]

            return cls(
                _check_predicate,
                f"exception_cause({getattr(predicate, '__name__', repr(predicate))})",
            )

        for type_ in exception_types_or_predicate:
            if not (
                isinstance(type_, type) and issubclass(type_, BaseException)
            ):
                msg = (
                    "Match.exception_cause() arguments must all be exception "
                    f"classes, got {type_!r}"
                )
                raise TypeError(msg)
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
            f"predicate({getattr(fn, '__name__', repr(fn))})",
        )

    # --- Negated forms (symmetric `not_*` prefix) ----------------------

    @classmethod
    def not_exception(
        cls,
        *exception_types_or_predicate: type[Exception]
        | Callable[[Exception], bool],
    ) -> Match:
        """Engage when the call did NOT raise a matching exception.

        Symmetric inverse of ``Match.exception``: same arguments,
        opposite verdict. A returned outcome (no exception raised)
        also engages.
        """
        return cls._invert_of(
            cls.exception(*exception_types_or_predicate),
            "not_exception",
            exception_types_or_predicate,
        )

    @classmethod
    def not_result(
        cls,
        value_or_predicate: Any | Callable[[Any], bool],  # noqa: ANN401
    ) -> Match:
        """Engage when the call did NOT return a matching value.

        Symmetric inverse of ``Match.result``: same argument,
        opposite verdict. A raised outcome (no value returned) also
        engages.
        """
        positive = cls.result(value_or_predicate)
        return cls(
            lambda outcome: not positive(outcome),
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

        Symmetric inverse of ``Match.exception_message``.
        """
        positive = cls.exception_message(contains=contains, regex=regex)
        return cls(
            lambda outcome: not positive(outcome),
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

        Symmetric inverse of ``Match.exception_cause``.
        """
        positive = cls.exception_cause(*exception_types_or_predicate)
        return cls(
            lambda outcome: not positive(outcome),
            positive._repr.replace(  # noqa: SLF001
                "exception_cause(", "not_exception_cause(", 1
            ),
        )

    @classmethod
    def _invert_of(
        cls,
        positive: Match,
        prefix: str,
        args: tuple[Any, ...],
    ) -> Match:
        """Build an inverted Match with a clean repr."""
        if (
            len(args) == 1
            and callable(args[0])
            and not isinstance(args[0], type)
        ):
            label = getattr(args[0], "__name__", repr(args[0]))
        else:
            label = ", ".join(
                t.__name__ if isinstance(t, type) else repr(t) for t in args
            )
        return cls(
            lambda outcome: not positive(outcome),
            f"{prefix}({label})",
        )
