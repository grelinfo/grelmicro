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
    from grelmicro.metrics._component import Metrics

_active: Metrics | None = None
"""The `Metrics` component currently inside its `async with` block, if any."""

_instruments: dict[str, Any] = {}
"""Cache of created instruments keyed by name, scoped to the active component."""


def activate(component: Metrics) -> None:
    """Mark `component` as the active metrics component.

    Called from `Metrics.__aenter__`. Clears the instrument cache so a new
    component lifecycle never reuses instruments bound to a torn-down
    `MeterProvider`.
    """
    global _active  # noqa: PLW0603
    _active = component
    _instruments.clear()


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
