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

_UNNAMEABLE = "<predicate>"
"""Stands in for a predicate that refuses every attempt to name it."""
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
    """Name a predicate by what it is, never by what it holds.

    A name, never a `repr`. A predicate carries caller data: a
    `functools.partial` holds the arguments bound into it, and an object
    predicate holds its attributes, so a `repr` would put an API key into
    a warning and into the policy's own `repr`.

    Naming reads caller-controlled code at every step: `__getattr__`, a
    `__name__` property, `str` of what it returns, and even
    `type(x).__name__` through a metaclass property.

    The result is normalized to an exact `str`. `str()` may return a
    subclass, and a subclass can run caller code again from `__format__`
    or `__str__` when the caller interpolates it.
    """
    try:
        try:
            name = getattr(predicate, "__name__", None)
            text = str(type(predicate).__name__) if name is None else str(name)
        except Exception:  # noqa: BLE001
            text = str(type(predicate).__name__)
        return str.__str__(text)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return _UNNAMEABLE


def _describe_value(value: Any) -> str:  # noqa: ANN401
    """Render a literal for a policy label, and never raise doing it.

    A literal is what the caller wrote into the policy, so `Match.result(200)`
    reads as itself. Falls back to the type name when `repr` refuses.
    """
    try:
        return str.__str__(repr(value))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return _describe(type(value))


def _warn(message: str, *args: Any) -> None:  # noqa: ANN401
    """Emit a warning, and never raise doing it.

    `Logger.handle` runs filters unguarded, and a filter is caller code: a
    request-context filter raising outside a request would escape from the
    very place a matcher is reporting a problem, replacing the error being
    handled.
    """
    try:
        _log.warning(message, *args)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return


def _is_instance(value: Any, parent: type) -> bool:  # noqa: ANN401
    """Return whether `value` is a `parent`, and never raise deciding it.

    `isinstance` reads `__class__` when the fast check fails, and a lazy
    proxy forwards that to an object which raises while unbound. A hostile
    `__instancecheck__` raises outright.
    """
    try:
        return isinstance(value, parent)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


def _is_class(candidate: Any) -> bool:  # noqa: ANN401
    """Return whether `candidate` is a class, and never raise deciding it."""
    return _is_instance(candidate, type)


def _is_subclass(candidate: Any, parent: type) -> bool:  # noqa: ANN401
    """Return whether `candidate` subclasses `parent`, and never raise.

    An object whose `__class__` reports `type` reaches `issubclass`, which
    refuses it. Answering False sends it to the argument error the caller
    is meant to see.
    """
    try:
        return issubclass(candidate, parent)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


def _message_of(exc: BaseException) -> str | None:
    """Return an exception's message, or None when it cannot be read.

    A driver exception that formats lazily from a closed connection raises
    from `__str__`. None rather than an empty string, because an empty
    string is matched by `contains=""` and by any pattern accepting it.
    """
    try:
        return str(exc)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return None


def _equals(left: Any, right: Any) -> bool:  # noqa: ANN401
    """Return whether two values compare equal, and never raise.

    `__eq__` is caller code: it can raise, or return something whose truth
    value raises. Neither is a match.
    """
    try:
        return bool(left == right)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


def _name_of_argument(value: Any) -> str:  # noqa: ANN401
    """Name a rejected argument by what it is, never by what it holds.

    A validator message is rendered verbatim, so echoing the value would
    put caller data into the error.
    """
    return _describe(value) if _is_class(value) else _describe(type(value))


def _is_exact_str(value: Any) -> bool:  # noqa: ANN401
    """Return whether `value` is a string, without trusting `__class__`."""
    return type(value) is str or (
        _is_class(type(value)) and _is_subclass(type(value), str)
    )


def _compile_pattern(regex: Any) -> re.Pattern[str]:  # noqa: ANN401
    """Return a compiled pattern, refusing anything else with a `ValueError`.

    `re.error` is not a `ValueError`, so an invalid pattern is converted
    here rather than escaping the validator conversion at match time.

    A pattern compiled from bytes is refused too: it can never match a
    message, which is always a string, so it would engage on nothing.
    """
    if _is_exact_str(regex):
        try:
            return re.compile(str.__str__(regex))
        except re.error as exc:
            msg = (
                f"Match.exception_message() regex= is not a valid regex: {exc}"
            )
            raise ValueError(msg) from None
    if _is_instance(regex, re.Pattern):
        pattern = cast("re.Pattern[Any]", regex)
        if not _is_exact_str(pattern.pattern):
            msg = (
                "Match.exception_message() regex= must be a string pattern, "
                "got a bytes pattern, which matches no message"
            )
            raise ValueError(msg)
        return cast("re.Pattern[str]", pattern)
    msg = (
        f"Match.exception_message() regex= must be a string or a compiled "
        f"pattern, got {_describe(type(regex))}"
    )
    # `ValueError` so pydantic wraps it, as elsewhere in this module.
    raise ValueError(msg)


def _already_warned(predicate: Any) -> bool:  # noqa: ANN401
    """Return whether this predicate was warned about, recording it if not.

    Held weakly, so a collected predicate stops speaking for the next one
    allocated at its address. One that a `WeakSet` cannot hold, or that a
    changing `__hash__` makes impossible to find again, is kept by address
    alongside a reference, and the oldest is dropped once
    `_UNTRACKABLE_LIMIT` is reached, so a long-lived predicate past the
    bound is reported again rather than the registry growing. The bound is
    approximate: threads racing the same eviction, or a `__del__` that
    re-enters, can carry it a little past the limit.

    Never raises. Bookkeeping runs where an error would replace the one
    the caller is handling, and `__hash__`, `__eq__` and `__del__` are all
    caller code. A failure here reports the predicate again, which is the
    harmless direction.
    """
    try:
        # Asked first: a predicate already known by address must not reach
        # the weak path again. Address and referent are held together, so
        # the address cannot have been handed to another predicate.
        if id(predicate) in _warned_untrackable:
            return True
        try:
            if predicate in _warned_predicates:
                return True
            _warned_predicates.add(predicate)
            if predicate in _warned_predicates:
                return False
            # A `__hash__` that answers differently each call never finds
            # its own entry again, so the set would grow one entry per
            # call. Remember it by address instead.
            _warned_predicates.discard(predicate)
        except Exception:  # noqa: BLE001, S110
            pass
        while len(_warned_untrackable) >= _UNTRACKABLE_LIMIT:
            oldest = next(iter(_warned_untrackable))
            _warned_untrackable.pop(oldest, None)
        _warned_untrackable[id(predicate)] = predicate
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False
    return False


def _coerce_bool(result: Any, predicate: Any) -> bool:  # noqa: ANN401
    """Return ``bool(result)``, warning if ``result`` was not already ``bool``.

    The warning fires once per predicate, so a tight retry loop reports it
    once rather than on every attempt.

    A value whose truth cannot be read counts as no match. Note the
    `not_*` forms negate that, so an undecidable value engages them.
    """
    try:
        coerced = bool(result)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        if not _already_warned(predicate):
            _warn(
                "Match predicate %s returned %s, whose truth value raised; "
                "reading it as no match.",
                _describe(predicate),
                _describe(type(result)),
            )
        return False
    if type(result) is not bool and not _already_warned(predicate):
        _warn(
            "Match predicate %s returned non-bool %s; coercing to bool. "
            "Return an explicit bool to suppress this warning.",
            _describe(predicate),
            _describe(type(result)),
        )
    return coerced


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
        """Test the outcome against this filter, and never raise.

        A matcher runs inside the resilience machinery, where `Retry`
        calls it from an `except` block, so anything raised here would
        replace the error the caller is already handling. A predicate that
        raises, a class with a hostile `__instancecheck__`, an exception
        whose `__cause__` raises: each reads as no match and is reported
        once.

        A real interrupt still propagates.
        """
        try:
            return self._matcher(outcome)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001
            if not _already_warned(self._matcher):
                _warn(
                    "Match %s raised while testing an outcome; reading it "
                    "as no match. A matcher runs where a raised error "
                    "replaces the one being handled.",
                    self._repr,
                )
            return False

    def __repr__(self) -> str:
        return f"Match.{self._repr}"

    def explain(self) -> str:
        """Return the human-readable matcher tree for debugging."""
        return repr(self)

    def __or__(self, other: Match) -> Match:
        """Return a Match that engages when either side engages."""
        if not _is_instance(other, Match):
            return NotImplemented
        return Match(
            lambda outcome: self(outcome) or other(outcome),
            f"any({self._repr}, {other._repr})",
        )

    def __and__(self, other: Match) -> Match:
        """Return a Match that engages when both sides engage."""
        if not _is_instance(other, Match):
            return NotImplemented
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
            and not _is_class(exception_types_or_predicate[0])
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
            if not (_is_class(type_) and _is_subclass(type_, Exception)):
                got = _name_of_argument(type_)
                msg = (
                    f"Match.exception() arguments must all be exception "
                    f"classes, got {got}"
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`, so a `TypeError` raised
                # inside a validator escapes every documented `except`, and
                # escapes `reconfigure_all` too.
                raise ValueError(msg)
        types = cast(
            "tuple[type[Exception], ...]", tuple(exception_types_or_predicate)
        )

        def _check_types(outcome: Outcome[Any]) -> bool:
            return outcome.raised and isinstance(outcome.exception, types)

        names = ", ".join(_describe(t) for t in types)
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
        if callable(value_or_predicate) and not _is_class(value_or_predicate):
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
            return not outcome.raised and _equals(outcome.result, value)

        return cls(_check_value, f"result({_describe_value(value)})")

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
            # `ValueError`, not `TypeError`: pydantic converts only
            # `ValueError` and `AssertionError`, so a `TypeError` raised
            # inside a validator escapes every documented `except`.
            raise ValueError(msg)

        if contains is not None:
            if not _is_exact_str(contains):
                msg = (
                    f"Match.exception_message() contains= must be a string, "
                    f"got {_describe(type(contains))}"
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`, so a `TypeError` raised
                # inside a validator escapes every documented `except`.
                raise ValueError(msg)
            needle = str.__str__(contains)

            def _check_contains(outcome: Outcome[Any]) -> bool:
                exception = outcome.exception
                if exception is None:
                    return False
                message = _message_of(exception)
                return message is not None and needle in message

            return cls(
                _check_contains, f"exception_message(contains={needle!r})"
            )

        # ``regex`` is non-None on this branch (mutual-exclusion check above).
        pattern = _compile_pattern(regex)

        def _check_regex(outcome: Outcome[Any]) -> bool:
            exception = outcome.exception
            if exception is None:
                return False
            message = _message_of(exception)
            return message is not None and pattern.search(message) is not None

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
            and not _is_class(exception_types_or_predicate[0])
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
            if not (_is_class(type_) and _is_subclass(type_, BaseException)):
                got = _name_of_argument(type_)
                msg = (
                    f"Match.exception_cause() arguments must all be exception "
                    f"classes, got {got}"
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`, so a `TypeError` raised
                # inside a validator escapes every documented `except`, and
                # escapes `reconfigure_all` too.
                raise ValueError(msg)
        types = cast(
            "tuple[type[BaseException], ...]",
            tuple(exception_types_or_predicate),
        )

        def _check_types(outcome: Outcome[Any]) -> bool:
            exc = outcome.exception
            if not outcome.raised or exc is None:
                return False
            return isinstance(exc.__cause__, types)

        names = ", ".join(_describe(t) for t in types)
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
        if not callable(fn):
            msg = (
                f"Match.predicate() fn must be callable, got "
                f"{_name_of_argument(fn)}"
            )
            # `ValueError`, not `TypeError`: pydantic converts only
            # `ValueError` and `AssertionError`, so a `TypeError` raised
            # inside a validator escapes every documented `except`.
            raise ValueError(msg)  # noqa: TRY004

        def _check(outcome: Outcome[Any]) -> bool:
            return _coerce_bool(fn(outcome), fn)

        return cls(
            _check,
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
