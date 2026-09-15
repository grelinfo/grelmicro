"""Process-global hub holding the active `Metrics` component.

The hub is the single source of truth the no-op emit helpers consult on
the hot path. When no `Metrics` component is active, `active()` returns
`None` and every emit helper returns immediately after one attribute
read. When a component is active, instruments are created once and
cached here keyed by name so repeated emits skip instrument creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from grelmicro.metrics._component import Metrics

_active: Metrics | None = None
"""The `Metrics` component currently inside its `async with` block, if any."""

_instruments: dict[str, Any] = {}
"""Cache of created instruments keyed by name, scoped to the active component."""

_observed: dict[str, tuple[Callable[[Any], Iterable[Any]], str]] = {}
"""Gauges read when metrics are collected, keyed by name, with their unit.

Kept across component lifecycles, so each `Metrics` that activates
creates them again on its own provider.
"""


def activate(component: Metrics) -> None:
    """Mark `component` as the active metrics component.

    Called from `Metrics.__aenter__`. Clears the instrument cache so a new
    component lifecycle never reuses instruments bound to a torn-down
    `MeterProvider`, then creates every observed gauge on the new one.
    """
    global _active  # noqa: PLW0603
    _active = component
    _instruments.clear()
    if not component._entered:  # noqa: SLF001
        return
    for name, (callback, unit) in _observed.items():
        _observe(component, name, callback, unit)


def deactivate(component: Metrics) -> None:
    """Clear the active component if it is `component`.

    Called from `Metrics.__aexit__`. A mismatched component (e.g. nested
    lifecycles restoring out of order) leaves the current active one
    untouched.
    """
    global _active  # noqa: PLW0603
    if _active is component:
        _active = None
        _instruments.clear()


def active() -> Metrics | None:
    """Return the active `Metrics` component, or `None`."""
    return _active


def get_instrument(name: str, factory: Any) -> Any:  # noqa: ANN401
    """Return the cached instrument for `name`, creating it via `factory`.

    `factory` is a zero-argument callable that builds the instrument. It is
    invoked at most once per `(name, component lifecycle)`.
    """
    instrument = _instruments.get(name)
    if instrument is None:
        instrument = factory()
        _instruments[name] = instrument
    return instrument


def observe_with(
    name: str,
    callback: Callable[[Any], Iterable[Any]],
    unit: str,
) -> None:
    """Register a gauge whose value `callback` reads when metrics are collected.

    For a value that changes without an event to report it, such as a ban
    running out. The gauge is created on the active component now, and on
    every component that activates later. Registering a name again
    replaces its callback for the components that activate after.
    """
    _observed[name] = (callback, unit)
    component = _active
    if component is not None and component._entered:  # noqa: SLF001
        _observe(component, name, callback, unit)


def _observe(
    component: Metrics,
    name: str,
    callback: Callable[[Any], Iterable[Any]],
    unit: str,
) -> None:
    """Create the observable gauge `name` on `component`, once per lifecycle."""
    get_instrument(
        name,
        lambda: component.meter("grelmicro.metrics").create_observable_gauge(
            name, callbacks=[callback], unit=unit
        ),
    )
