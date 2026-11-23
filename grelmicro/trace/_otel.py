"""Lazy access to the optional `opentelemetry` package.

`opentelemetry` is an extra. `import grelmicro.trace` must not pull it
in: production apps that never configure tracing should not pay the
import cost. The package is resolved on first call to `get` and cached
for subsequent calls via `functools.cache`.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from opentelemetry.trace import Span, StatusCode, Tracer, TracerProvider

    class _OTelTrace(Protocol):
        def get_tracer(self, instrumenting_module_name: str) -> Tracer: ...
        def get_current_span(self) -> Span: ...
        def get_tracer_provider(self) -> TracerProvider: ...


class OTel(NamedTuple):
    """Resolved opentelemetry handles.

    Both handles come from the same import, so an instance always
    carries both. Absence is `None` in place of the whole tuple.
    """

    trace: _OTelTrace
    status_code: type[StatusCode]


@cache
def get() -> OTel | None:
    """Return resolved opentelemetry handles, or `None` when not installed.

    Cached after first call.
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.trace import StatusCode  # noqa: PLC0415
    except ImportError:
        return None
    return OTel(trace, StatusCode)


class GlobalTracer:
    """A tracer of the global tracer provider, fetched again when it changes.

    `get` returns a tracer of the provider installed at that moment, so spans
    reach the provider that replaced an earlier one. The tracer is kept
    until the global provider changes.
    """

    __slots__ = ("_bound", "_name", "_trace")

    def __init__(self, trace: _OTelTrace, name: str) -> None:
        """Bind the `opentelemetry.trace` module and the tracer's name."""
        self._trace = trace
        self._name = name
        self._bound: tuple[TracerProvider, Tracer] | None = None

    def get(self) -> Tracer:
        """Return a tracer of the global provider installed now."""
        provider = self._trace.get_tracer_provider()
        bound = self._bound
        if bound is None or bound[0] is not provider:
            bound = (provider, provider.get_tracer(self._name))
            self._bound = bound
        return bound[1]


def exceptions_on_spans() -> bool:
    """Return whether exceptions go on spans, per `OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN`.

    `logs` moves them to the logs signal. `logs/dup` and no value keep them
    on spans.
    """
    choice = os.environ.get("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", "")
    return choice.strip().lower() != "logs"
