"""Lazy access to the optional `opentelemetry` metrics package.

`opentelemetry` is an extra. `import grelmicro.metrics` must not pull it
in: production apps that never configure metrics should not pay the
import cost. The package is resolved on first call to `get` and cached
for subsequent calls via `functools.cache`.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter


class _OTelMetrics(Protocol):
    def get_meter(self, name: str) -> Meter: ...


class OTel(NamedTuple):
    """Resolved opentelemetry metrics handle, or `None` when not installed."""

    metrics: _OTelMetrics | None


@cache
def get() -> OTel:
    """Return resolved opentelemetry metrics handle. Cached after first call."""
    try:
        from opentelemetry import metrics  # noqa: PLC0415
    except ImportError:
        return OTel(None)
    return OTel(metrics)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
