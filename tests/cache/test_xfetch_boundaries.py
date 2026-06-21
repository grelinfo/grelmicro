"""Boundary tests for the XFetch early-refresh helpers.

These pin the probabilistic-refresh threshold and the early-window bounds in
`cached`, so a flipped comparison or the `or 1.0` random fallback is caught.
"""

from __future__ import annotations

import importlib
import math
from typing import TYPE_CHECKING

from grelmicro.cache.cached import (
    _due_for_early_refresh,
    _xfetch_should_refresh,
)

if TYPE_CHECKING:
    import pytest

# The package re-exports the `cached` function under the same dotted path, so
# `import grelmicro.cache.cached` resolves to the function. Fetch the module.
cached_module = importlib.import_module("grelmicro.cache.cached")

_TTL = 10.0
_EARLY = 0.5  # early window is 0.5 * 10 = 5.0 seconds
_NOW = 100.0
_BIG_DELTA = 100.0  # makes the XFetch die roll always fire inside the window


def test_xfetch_threshold_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`delta * -ln(rand) >= remaining` fires at exact equality."""
    monkeypatch.setattr(cached_module, "_random", lambda: 0.5)
    delta = 1.0
    remaining = delta * -math.log(0.5)  # exactly the threshold
    assert _xfetch_should_refresh(remaining, delta) is True


def test_xfetch_zero_random_falls_back_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero random rolls back to 1.0, giving a -ln of 0."""
    monkeypatch.setattr(cached_module, "_random", lambda: 0.0)
    # rand = 0.0 or 1.0 = 1.0 -> -ln(1) = 0 -> 0 >= 0 is True.
    assert _xfetch_should_refresh(0.0, 5.0) is True


def _meta_for_remaining(remaining: float) -> tuple[float, float]:
    """Build XFetch meta `(written, delta)` for a target remaining life."""
    written = _NOW + remaining - _TTL
    return (written, _BIG_DELTA)


def test_early_window_upper_bound_is_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remaining == early * ttl` is still inside the window."""
    monkeypatch.setattr(cached_module, "_now", lambda: _NOW)
    monkeypatch.setattr(cached_module, "_random", lambda: 0.0001)
    meta = _meta_for_remaining(_EARLY * _TTL)  # remaining == 5.0
    assert _due_for_early_refresh(meta, _TTL, _EARLY) is True


def test_early_window_includes_one_second_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One second of life left is inside the window, not rejected."""
    monkeypatch.setattr(cached_module, "_now", lambda: _NOW)
    monkeypatch.setattr(cached_module, "_random", lambda: 0.0001)
    meta = _meta_for_remaining(1.0)
    assert _due_for_early_refresh(meta, _TTL, _EARLY) is True


def test_zero_remaining_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired entry (remaining == 0) never early-refreshes."""
    monkeypatch.setattr(cached_module, "_now", lambda: _NOW)
    monkeypatch.setattr(cached_module, "_random", lambda: 0.0001)
    meta = _meta_for_remaining(0.0)
    assert _due_for_early_refresh(meta, _TTL, _EARLY) is False
