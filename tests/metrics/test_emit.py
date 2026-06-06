"""Tests for the internal emit helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from tests.metrics.conftest import MetricsHarness


def test_emit_noop_when_off() -> None:
    """Every helper returns silently when no component is active."""
    _emit.record_duration("x.duration", 1.0)
    _emit.incr("x.calls")
    _emit.observe("x.state", 3)
    _emit.add_up_down("x.active", 1)


def test_record_duration(metrics_reader: MetricsHarness) -> None:
    """`record_duration` writes a histogram value with attributes."""
    _emit.record_duration("svc.duration", 0.25, outcome="success")
    points = metrics_reader.points("svc.duration")
    assert len(points) == 1
    value, attrs = points[0]
    assert value == 0.25  # noqa: PLR2004
    assert attrs == {"outcome": "success"}


def test_incr_default_and_custom(metrics_reader: MetricsHarness) -> None:
    """`incr` adds to a counter, default amount 1 and custom amounts."""
    _emit.incr("svc.calls", outcome="success")
    _emit.incr("svc.calls", 4, outcome="success")
    points = metrics_reader.points("svc.calls")
    assert len(points) == 1
    value, attrs = points[0]
    assert value == 5  # noqa: PLR2004
    assert attrs == {"outcome": "success"}


def test_observe_and_add_up_down(metrics_reader: MetricsHarness) -> None:
    """`observe` and `add_up_down` both feed an up_down_counter."""
    _emit.add_up_down("svc.active", 1)
    _emit.add_up_down("svc.active", 1)
    _emit.add_up_down("svc.active", -1)
    points = metrics_reader.points("svc.active")
    assert points[0][0] == 1


def test_no_attrs_passes_none(metrics_reader: MetricsHarness) -> None:
    """A call with no attributes still records a point."""
    _emit.incr("svc.calls")
    points = metrics_reader.points("svc.calls")
    assert points[0][1] == {}
