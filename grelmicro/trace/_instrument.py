"""Instrument decorator: wraps a function to emit an OTel span and attach structured log context."""

from __future__ import annotations

import functools
import inspect
import sys
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, overload

from grelmicro._context import pop_context as _pop_context
from grelmicro._context import push_context as _push_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode
except ImportError:  # pragma: no cover
    trace: Any = None  # type: ignore[no-redef]
    StatusCode: Any = None  # type: ignore[no-redef,misc]

P = ParamSpec("P")
R = TypeVar("R")

_IMPLICIT_PARAMS = frozenset({"self", "cls"})


def _record_exception(otel_span: object) -> None:
    """Record the current exception on an OTel span."""
    if (  # type: ignore[truthy-function]
        otel_span is not None
        and hasattr(otel_span, "is_recording")
        and otel_span.is_recording()  # ty: ignore[call-non-callable]
    ):
        exc = sys.exc_info()[1]
        if exc is not None:
            otel_span.set_status(StatusCode.ERROR, str(exc))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
            otel_span.record_exception(exc)  # ty: ignore[unresolved-attribute]


def _make_extract_fields(
    sig: inspect.Signature,
    skip_set: AbstractSet[str],
    *,
    skip_all: bool,
) -> Callable[..., dict[str, Any]]:
    """Create a field extractor closure for the given signature."""

    def _extract_fields(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if skip_all:
            return {}
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
        except (TypeError, ValueError):
            return {}
        return {
            k: v
            for k, v in bound.arguments.items()
            if k not in skip_set and k not in _IMPLICIT_PARAMS
        }

    return _extract_fields


@overload
def instrument[**P, R](func: Callable[P, R]) -> Callable[P, R]: ...


@overload
def instrument(
    *,
    name: str | None = None,
    skip: AbstractSet[str] | None = None,
    skip_all: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def instrument[**P, R](  # type: ignore[return-value]
    func: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    skip: AbstractSet[str] | None = None,
    skip_all: bool = False,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Instrument a function with span context and logging enrichment.

    Creates an OTel span (if tracing configured) and pushes function
    arguments as context fields into log records. Like Rust's ``#[instrument]``.

    When an exception propagates, the OTel span is marked as ERROR and
    the exception is recorded on the span.

    Note:
        Argument values are stringified (``str()``) when sent to OTel.
        Use ``skip`` for arguments whose string representation may contain
        sensitive data.

    Args:
        func: The function to instrument (set automatically for bare decorator).
        name: Custom span name. Defaults to the function's qualified name.
        skip: Argument names to exclude from context (any set-like collection).
        skip_all: If True, do not record any arguments.
    """
    skip_set = skip or frozenset()

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = name or getattr(fn, "__qualname__", str(fn))
        extract = _make_extract_fields(
            inspect.signature(fn), skip_set, skip_all=skip_all
        )
        tracer = trace.get_tracer(fn.__module__) if trace is not None else None

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                fields = extract(args, kwargs)
                token = _push_context(fields)
                try:
                    if tracer is not None:
                        with tracer.start_as_current_span(
                            span_name,
                            attributes={k: str(v) for k, v in fields.items()},
                        ) as otel_span:
                            try:
                                return await fn(*args, **kwargs)  # type: ignore[misc]
                            except BaseException:
                                _record_exception(otel_span)
                                raise
                    return await fn(*args, **kwargs)  # type: ignore[misc]
                finally:
                    _pop_context(token)

            return async_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            fields = extract(args, kwargs)
            token = _push_context(fields)
            try:
                if tracer is not None:
                    with tracer.start_as_current_span(
                        span_name,
                        attributes={k: str(v) for k, v in fields.items()},
                    ) as otel_span:
                        try:
                            return fn(*args, **kwargs)
                        except BaseException:
                            _record_exception(otel_span)
                            raise
                return fn(*args, **kwargs)
            finally:
                _pop_context(token)

        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)

    return decorator
