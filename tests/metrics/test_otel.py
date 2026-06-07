"""Tests for the lazy `opentelemetry` metrics accessor."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

from grelmicro.metrics import _otel

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def test_get_returns_metrics_handle() -> None:
    """With opentelemetry installed, `get()` exposes the metrics api."""
    _otel.get.cache_clear()
    otel = _otel.get()
    assert otel.metrics is not None
    assert hasattr(otel.metrics, "get_meter")
    _otel.get.cache_clear()


def test_get_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `opentelemetry` cannot be imported, `get()` yields None."""
    _otel.get.cache_clear()
    real_import: Callable[..., Any] = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    otel = _otel.get()
    assert otel.metrics is None
    _otel.get.cache_clear()
