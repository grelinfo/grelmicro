"""Instrument decorator inspired by Rust's tracing #[instrument]."""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, overload

from grelmicro.tracing._context import _pop_context, _push_context

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace: Any = None  # type: ignore[no-redef]

P = ParamSpec("P")
R = TypeVar("R")


@overload
def instrument(func: Callable[P, R]) -> Callable[P, R]: ...


@overload
def instrument(
    *,
    name: str | None = None,
    skip: set[str] | None = None,
    skip_all: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def instrument(  # type: ignore[return-value]
    func: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    skip: set[str] | None = None,
    skip_all: bool = False,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Instrument a function with span context and logging enrichment.

    Creates an OTel span (if tracing configured) and pushes function
    arguments as context fields into log records. Like Rust's ``#[instrument]``.

    Can be used as a bare decorator or with arguments::

        @instrument
        async def process(order_id: str, user_id: str):
            logger.info("started")  # auto-includes order_id, user_id

        @instrument(skip={"password"})
        def login(username: str, password: str): ...

        @instrument(skip_all=True)
        def bulk_process(payload: bytes): ...

    Args:
        func: The function to instrument (set automatically for bare decorator).
        name: Custom span name. Defaults to the function's qualified name.
        skip: Set of argument names to exclude from context.
        skip_all: If True, do not record any arguments.
    """
    skip_set = skip or set()

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = name or getattr(fn, "__qualname__", str(fn))
        sig = inspect.signature(fn)
        tracer = trace.get_tracer(fn.__module__) if trace is not None else None

        def _extract_fields(
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> dict[str, Any]:
            if skip_all:
                return {}
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return {
                k: v
                for k, v in bound.arguments.items()
                if k not in skip_set and k != "self"
            }

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                fields = _extract_fields(args, kwargs)
                token = _push_context(fields)
                try:
                    if tracer is not None:
                        with tracer.start_as_current_span(
                            span_name,
                            attributes={k: str(v) for k, v in fields.items()},
                        ):
                            return await fn(*args, **kwargs)  # type: ignore[misc]
                    return await fn(*args, **kwargs)  # type: ignore[misc]
                finally:
                    _pop_context(token)

            return async_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            fields = _extract_fields(args, kwargs)
            token = _push_context(fields)
            try:
                if tracer is not None:
                    with tracer.start_as_current_span(
                        span_name,
                        attributes={k: str(v) for k, v in fields.items()},
                    ):
                        return fn(*args, **kwargs)
                return fn(*args, **kwargs)
            finally:
                _pop_context(token)

        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)

    return decorator
