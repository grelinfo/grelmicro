"""Instrument decorator inspired by Rust's tracing #[instrument]."""

import functools
import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from opentelemetry import trace

P = ParamSpec("P")
R = TypeVar("R")


@overload
def instrument(func: Callable[P, R]) -> Callable[P, R]: ...


@overload
def instrument(
    *,
    span_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    record_args: bool = True,
    skip: set[str] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def instrument(
    func: Callable[P, R] | None = None,
    *,
    span_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    record_args: bool = True,
    skip: set[str] | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Instrument a function with an OpenTelemetry span.

    Inspired by Rust's tracing `#[instrument]` macro. Automatically creates
    a span for the function call and optionally records function arguments
    as span attributes.

    Can be used as a bare decorator or with arguments:

        @instrument
        def my_function(x: int, y: str): ...

        @instrument(span_name="custom", skip={"password"})
        def login(username: str, password: str): ...

    Args:
        func: The function to instrument (set automatically when used
            as a bare decorator).
        span_name: Custom span name. Defaults to the function's
            qualified name.
        attributes: Additional span attributes to set.
        record_args: Whether to record function arguments as span
            attributes. Default: True.
        skip: Set of argument names to exclude from recording.
    """
    skip = skip or set()

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        tracer = trace.get_tracer(fn.__module__)
        name = span_name or fn.__qualname__
        sig = inspect.signature(fn)

        def _record_arguments(
            span: trace.Span,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> None:
            if not record_args:
                return
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for param_name, value in bound.arguments.items():
                if param_name in skip or param_name == "self":
                    continue
                span.set_attribute(
                    f"arg.{param_name}", str(value)
                )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(
                *args: P.args, **kwargs: P.kwargs
            ) -> R:
                with tracer.start_as_current_span(
                    name,
                    attributes=attributes or {},
                ) as span:
                    _record_arguments(span, args, kwargs)
                    return await fn(*args, **kwargs)  # type: ignore[misc]

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer.start_as_current_span(
                name,
                attributes=attributes or {},
            ) as span:
                _record_arguments(span, args, kwargs)
                return fn(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)

    return decorator
