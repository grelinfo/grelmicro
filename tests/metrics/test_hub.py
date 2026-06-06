"""Tests for the process-global metrics hub."""

from __future__ import annotations

from grelmicro.metrics import _hub
from grelmicro.metrics._component import Metrics


def test_deactivate_ignores_other_component() -> None:
    """Deactivating a non-active component leaves the active one in place."""
    active = Metrics()
    other = Metrics()
    _hub.activate(active)
    try:
        _hub.deactivate(other)
        assert _hub.active() is active
    finally:
        _hub.deactivate(active)
    assert _hub.active() is None


def test_get_instrument_caches() -> None:
    """`get_instrument` invokes the factory once and caches the result."""
    active = Metrics()
    _hub.activate(active)
    calls = []

    def factory() -> object:
        sentinel = object()
        calls.append(sentinel)
        return sentinel

    try:
        first = _hub.get_instrument("x", factory)
        second = _hub.get_instrument("x", factory)
        assert first is second
        assert len(calls) == 1
    finally:
        _hub.deactivate(active)
