"""Measure decorator: wraps a function to emit duration, call, and in-flight metrics."""

from __future__ import annotations

import functools
import inspect
import time
from typing import TYPE_CHECKING, Annotated, Any, ParamSpec, TypeVar, overload

from typing_extensions import Doc

from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from collections.abc import Callable

P = ParamSpec("P")
R = TypeVar("R")


def _default_name(fn: Callable[..., Any]) -> str:
    """Derive a metric base name from a function's module and qualname.

    Lowercased and dotted, e.g. `myapp.service.charge`. Inner-function
    markers (`<locals>`) are dropped so nested helpers stay readable.
    """
    module = getattr(fn, "__module__", "") or ""
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", str(fn)))
    qualname = qualname.replace(".<locals>", "")
    parts = [p for p in (module, qualname) if p]
    return ".".join(parts).lower()


class _Instruments:
    """The three metric names bound once at decoration time."""

    __slots__ = ("active", "calls", "duration", "in_flight")

    def __init__(self, base: str, *, in_flight: bool) -> None:
        """Bind the per-function metric names."""
        self.duration = f"{base}.duration"
        self.calls = f"{base}.calls"
        self.active = f"{base}.active"
        self.in_flight = in_flight

    def enter(self) -> float:
        """Mark a call start, raise the in-flight gauge, return the clock."""
        if self.in_flight:
            _emit.add_up_down(self.active, 1)
        return time.perf_counter()

    def success(self) -> None:
        """Record a success-outcome call."""
        _emit.incr(self.calls, outcome="success")

    def error(self, exc: BaseException) -> None:
        """Record an error-outcome call carrying the exception type."""
        _emit.incr(
            self.calls,
            outcome="error",
            **{"error.type": type(exc).__name__},
        )

    def exit(self, start: float) -> None:
        """Record the duration and lower the in-flight gauge."""
        _emit.record_duration(self.duration, time.perf_counter() - start)
        if self.in_flight:
            _emit.add_up_down(self.active, -1)


@overload
def measure[**P, R](func: Callable[P, R]) -> Callable[P, R]: ...


@overload
def measure(
    *,
    name: str | None = None,
    record_in_flight: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def measure[**P, R](  # type: ignore[return-value]
    func: Annotated[
        Callable[P, R] | None,
        Doc(
            """
            The function to measure. Bound automatically when `@measure`
            is used bare. `None` when called with arguments (`@measure(...)`)
            so the inner decorator can wrap the target.
            """,
        ),
    ] = None,
    *,
    name: Annotated[
        str | None,
        Doc(
            """
            Override the metric base name. Defaults to the wrapped
            function's module and qualified name, lowercased and dotted.
            """,
        ),
    ] = None,
    record_in_flight: Annotated[
        bool,
        Doc(
            """
            Also emit a `<name>.active` up_down_counter that rises while
            the function runs and falls when it returns or raises.
            """,
        ),
    ] = False,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Measure a function's duration, call count, and optional in-flight gauge.

    Emits three metrics, all no-ops when no `Metrics` component is active:

    - `<name>.duration`: a histogram of wall-clock seconds.
    - `<name>.calls`: a counter with an `outcome` attribute (`success`
      or `error`). On failure an `error.type` attribute carries the
      exception class name.
    - `<name>.active`: an up_down_counter present only when
      `record_in_flight=True`. Rises on entry, falls on exit.

    The base name defaults to the function's module and qualified name,
    lowercased and dotted. Works on sync and async functions.

    Args:
        func: The function to measure (set automatically for bare decorator).
        name: Custom base name. Defaults to the function's dotted qualname.
        record_in_flight: Emit a `<name>.active` in-flight gauge.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        base = name or _default_name(fn)
        m = _Instruments(base, in_flight=record_in_flight)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                start = m.enter()
                try:
                    result = await fn(*args, **kwargs)  # type: ignore[misc]
                except BaseException as exc:
                    m.error(exc)
                    raise
                else:
                    m.success()
                    return result
                finally:
                    m.exit(start)

            return async_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = m.enter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                m.error(exc)
                raise
            else:
                m.success()
                return result
            finally:
                m.exit(start)

        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)

    return decorator
