"""Internal emit helpers wired into each component's hot path.

Every helper is a no-op when no `Metrics` component is active or when the
`opentelemetry` extra is absent. The hot path is a single truthiness
check on the hub's active component, then return. Instruments are created
once on first use and cached in the hub keyed by name, so repeated emits
skip instrument creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grelmicro.metrics import _hub

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, UpDownCounter


def record_duration(name: str, seconds: float, /, **attrs: Any) -> None:  # noqa: ANN401
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
    histogram.record(seconds, attributes=attrs or None)


def incr(name: str, amount: int = 1, /, **attrs: Any) -> None:  # noqa: ANN401
    """Add `amount` to the `<name>` counter.

    No-op when no `Metrics` component is active.
    """
    component = _hub.active()
    if component is None:
        return
    counter: Counter = _hub.get_instrument(
        name,
        lambda: component.counter(name, unit="1"),
    )
    counter.add(amount, attributes=attrs or None)


def observe(name: str, amount: int, /, **attrs: Any) -> None:  # noqa: ANN401
    """Set a gauge-like value on the `<name>` up_down_counter.

    Used for snapshot values (a state code, an up/down flag). No-op when
    no `Metrics` component is active.
    """
    add_up_down(name, amount, **attrs)


def add_up_down(name: str, amount: int, /, **attrs: Any) -> None:  # noqa: ANN401
    """Add a signed `amount` to the `<name>` up_down_counter.

    Used for in-flight gauges that rise on entry and fall on exit. No-op
    when no `Metrics` component is active.
    """
    component = _hub.active()
    if component is None:
        return
    udc: UpDownCounter = _hub.get_instrument(
        name,
        lambda: component.up_down_counter(name, unit="1"),
    )
    udc.add(amount, attributes=attrs or None)
