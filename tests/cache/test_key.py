"""Tests for cache key generation order-independence and typing."""

from __future__ import annotations

import hashlib

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


def test_untyped_key_has_exact_module_qualname_digest_format() -> None:
    """The key is `{module}.{qualname}:{sha256(repr((args, kwargs)))}`."""
    args = (7, "x")
    kwargs = {"b": 2, "a": 1}
    raw = repr((args, sorted(kwargs.items())))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    expected = f"{_func.__module__}.{_func.__qualname__}:{digest}"
    assert make_cache_key(_func, args, kwargs) == expected


def test_typed_key_has_exact_format_with_types_appended() -> None:
    """`typed=True` appends `repr((arg_types, kwarg_types))` to the digest."""
    args = (7, "x")
    kwargs = {"b": 2, "a": 1}
    raw = repr((args, sorted(kwargs.items())))
    arg_types = tuple(type(value) for value in args)
    kwarg_types = tuple(type(value) for _, value in sorted(kwargs.items()))
    raw += repr((arg_types, kwarg_types))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    expected = f"{_func.__module__}.{_func.__qualname__}:{digest}"
    assert make_cache_key(_func, args, kwargs, typed=True) == expected


def test_typed_default_is_false() -> None:
    """No `typed` argument means untyped, so the key matches `typed=False`."""
    assert make_cache_key(_func, (3,), {}) == make_cache_key(
        _func, (3,), {}, typed=False
    )


def test_typed_keeps_argument_values_in_the_digest() -> None:
    """Same types, different values still give different typed keys.

    Guards against the digest dropping `repr((args, kwargs))` and hashing
    only the type tuples (`raw =` instead of `raw +=`).
    """
    first = make_cache_key(_func, (7,), {}, typed=True)
    second = make_cache_key(_func, (8,), {}, typed=True)
    assert first != second


def test_typed_distinguishes_kwarg_value_types() -> None:
    """A kwarg of `3` and `3.0` produce different typed keys.

    Guards against the kwarg type tuple being dropped or fixed to one type.
    """
    as_int = make_cache_key(_func, (), {"n": 3}, typed=True)
    as_float = make_cache_key(_func, (), {"n": 3.0}, typed=True)
    assert as_int != as_float
