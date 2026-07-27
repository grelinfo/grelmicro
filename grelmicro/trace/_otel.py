"""Lazy access to the optional `opentelemetry` package.

`opentelemetry` is an extra. `import grelmicro.trace` must not pull it
in: production apps that never configure tracing should not pay the
import cost. The package is resolved on first call to `get` and cached
for subsequent calls via `functools.cache`.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from opentelemetry.trace import Span, StatusCode, Tracer

    class _OTelTrace(Protocol):
        def get_tracer(self, instrumenting_module_name: str) -> Tracer: ...
        def get_current_span(self) -> Span: ...


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
