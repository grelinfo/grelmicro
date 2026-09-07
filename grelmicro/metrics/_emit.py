"""Internal emit helpers wired into each component's hot path.

Every helper is a no-op when no `Metrics` component is active or when the
`opentelemetry` extra is absent. The hot path is a single truthiness
check on the hub's active component, then return. Instruments are created
once on first use and cached in the hub keyed by name, so repeated emits
skip instrument creation.

Attributes are passed as one mapping rather than keyword arguments. Every
attribute grelmicro sets carries a dotted namespace, which is not a Python
identifier, and a caller that holds a constant mapping on the instance
passes it without building a dict per emit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from grelmicro.metrics import _hub

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, UpDownCounter

type Attributes = Mapping[str, Any] | None


def record_duration(
    name: str,
    seconds: float,
    attributes: Attributes = None,
    /,
) -> None:
    """Record a duration in seconds on the `<name>` histogram.

    No-op when no `Metrics` component is active.
    """
    component = _hub.active()
    if component is None:
        return
    histogram: Histogram = _hub.get_instrument(
        name,
        lambda: component.histogram(name, unit="s"),
    )
    histogram.record(seconds, attributes=attributes)


def incr(
    name: str,
    attributes: Attributes = None,
    /,
    amount: int = 1,
    unit: str = "1",
) -> None:
    """Add `amount` to the `<name>` counter.

    `unit` is read only when the instrument is created, and takes the
    annotation form the semantic conventions use for a count of discrete
    things, such as `{run}`.

    No-op when no `Metrics` component is active.
    """
    component = _hub.active()
    if component is None:
        return
    counter: Counter = _hub.get_instrument(
        name,
        lambda: component.counter(name, unit=unit),
    )
    counter.add(amount, attributes=attributes)


def observe(
    name: str,
    amount: float,
    attributes: Attributes = None,
    /,
    unit: str = "1",
) -> None:
    """Set the last-known value on the `<name>` gauge.

    Used for snapshot values (a state code, an up/down flag, the instant
    of the next run). Unlike `add_up_down`, the gauge records the value
    as-is rather than accumulating. No-op when no `Metrics` component is
    active.
    """
    component = _hub.active()
    if component is None:
        return
    gauge = _hub.get_instrument(
        name,
        lambda: component.gauge(name, unit=unit),
    )
    gauge.set(amount, attributes=attributes)


def add_up_down(
    name: str,
    amount: int,
    attributes: Attributes = None,
    /,
    unit: str = "1",
) -> None:
    """Add a signed `amount` to the `<name>` up_down_counter.

    Used for in-flight gauges that rise on entry and fall on exit. No-op
    when no `Metrics` component is active.
    """
    component = _hub.active()
    if component is None:
        return
    udc: UpDownCounter = _hub.get_instrument(
        name,
        lambda: component.up_down_counter(name, unit=unit),
    )
    udc.add(amount, attributes=attributes)
