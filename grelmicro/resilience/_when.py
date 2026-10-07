"""Shared `when=` input shape and its coercion to a `Match`."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Annotated, Any, cast

from pydantic import PlainSerializer, PlainValidator

from grelmicro._config import parse_csv_or_json
from grelmicro._guards import (
    is_class,
    is_instance,
    is_subclass,
    items_of,
    type_name,
)
from grelmicro.resilience._match import Match, named_classes

WhenInput = (
    Match
    | type[Exception]
    | tuple[type[Exception], ...]
    | Callable[[Exception], bool]
)
"""User-facing shape accepted by ``when=``.

A [`Match`][grelmicro.resilience.Match] instance, or one of the
shorthand forms a Match would build for you: a single exception
class, a tuple of classes, or a callable predicate on the
exception. Bare shapes are coerced to ``Match.exception(...)``.
"""


def _coerce_shorthand(value: Any) -> Match:  # noqa: ANN401
    """Coerce a class, a tuple of classes or a predicate to a ``Match``."""
    if is_class(value) and is_subclass(value, Exception):
        return Match.exception(value)
    if is_instance(value, tuple):
        items = items_of(value)
        if items is not None and all(
            is_class(item) and is_subclass(item, Exception) for item in items
        ):
            return Match.exception(*items)
    if callable(value):
        return Match.exception(value)
    msg = (
        "when= must be a Match, an Exception class, a tuple of "
        f"Exception classes, or a callable. Got {type_name(value)}"
    )
    # A type error, raised as `ValueError` so pydantic reports it.
    raise ValueError(msg)


def _resolve_fqn(fqn: str) -> type[Exception]:
    """Resolve a fully-qualified name to an Exception class."""
    module_path, _, name = fqn.rpartition(".")
    if not module_path:
        msg = (
            "when= env entry must be a fully-qualified name, "
            "such as 'httpx.HTTPError'"
        )
        raise ValueError(msg)
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        msg = "when= env entry names a module that cannot be imported"
        raise ValueError(msg) from exc
    try:
        cls = getattr(module, name)
    except AttributeError as exc:
        msg = "when= env entry names an attribute its module does not define"
        raise ValueError(msg) from exc
    if not (is_class(cls) and is_subclass(cls, Exception)):
        msg = "when= env entry does not name an Exception subclass"
        raise ValueError(msg)
    return cls


def coerce_when(value: Any) -> Match:  # noqa: ANN401
    """Coerce a ``when=`` value, or its environment string, to a ``Match``.

    Accepts a ``Match`` directly, an exception class, a tuple of
    classes, a callable predicate on the exception, or a CSV/JSON
    string or list of fully-qualified names (e.g. ``"httpx.HTTPError"``).

    Raises:
        ValueError: If the value has none of the accepted shapes.
    """
    if is_instance(value, Match):
        return value
    if is_instance(value, str):
        value = parse_csv_or_json(value)
    items = items_of(value) if is_instance(value, list | tuple) else None
    if items is not None and not (
        is_instance(value, tuple)
        and all(
            is_class(item) and is_subclass(item, Exception) for item in items
        )
    ):
        resolved: tuple[type[Exception], ...] = tuple(
            _resolve_fqn(item) if is_instance(item, str) else item
            for item in items
        )
        if not resolved:
            msg = "when= is empty, name at least one exception class"
            raise ValueError(msg)
        return Match.exception(*resolved)
    return _coerce_shorthand(value)


def _name_read_back(cls: type[Exception]) -> str | None:
    """Return the fully-qualified name that reads back to `cls`, if any.

    A class nested in another or defined in a function, or one its name
    resolves to another object, has none.
    """
    qualname = cls.__qualname__
    if "." in qualname:
        return None
    name = f"{cls.__module__}.{qualname}"
    try:
        resolved = _resolve_fqn(name)
    except ValueError:
        return None
    return name if resolved is cls else None


def dump_when(match: Match) -> list[str] | str:
    """Return the JSON form of an outcome filter.

    A filter that names module-level exception classes dumps as their
    fully-qualified names, the form `coerce_when` reads back. Any other
    filter dumps as its `repr`.
    """
    classes = named_classes(match)
    if classes is None:
        return repr(match)
    names = [_name_read_back(cls) for cls in classes]
    if None in names:
        return repr(match)
    return cast("list[str]", names)


OutcomeFilter = Annotated[
    Match,
    PlainValidator(coerce_when, json_schema_input_type=str | list[str]),
    PlainSerializer(dump_when, when_used="json"),
]
"""A config field holding an outcome filter, coerced by `coerce_when`."""
