"""Lazy access to the optional `opentelemetry` package.

`opentelemetry` is an extra. `import grelmicro.trace` must not pull it
in: production apps that never configure tracing should not pay the
import cost. The package is resolved on first call to `get` and cached
for subsequent calls via `functools.cache`.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

if TYPE_CHECKING:
    from opentelemetry.trace import Span, StatusCode, Tracer


class _OTelTrace(Protocol):
    def get_tracer(self, instrumenting_module_name: str) -> Tracer: ...
    def get_current_span(self) -> Span: ...


class OTel(NamedTuple):
    """Resolved opentelemetry handles, or `None` when not installed."""

    trace: _OTelTrace | None
    status_code: type[StatusCode] | None


@cache
def get() -> OTel:
    """Return resolved opentelemetry handles. Cached after first call."""
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.trace import StatusCode  # noqa: PLC0415
    except ImportError:
        return OTel(None, None)
    return OTel(cast("_OTelTrace", trace), StatusCode)
