"""Tests for cache key generation order-independence and typing."""

from __future__ import annotations

from grelmicro.cache._key import make_cache_key


def _func() -> None:
    """Stand-in function whose identity seeds the key."""


def test_kwarg_order_does_not_change_key() -> None:
    """Keyword arguments are sorted, so their order does not matter."""
    first = make_cache_key(_func, (), {"a": 1, "b": 2})
    second = make_cache_key(_func, (), {"b": 2, "a": 1})
    assert first == second


def test_typed_changes_the_key() -> None:
    """`typed=True` folds argument types into the key, changing it."""
    untyped = make_cache_key(_func, (3,), {}, typed=False)
    typed = make_cache_key(_func, (3,), {}, typed=True)
    assert typed != untyped
