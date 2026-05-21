"""Cache key helpers tests."""

from __future__ import annotations

from grelmicro.resilience.shield._key import default_cache_key, stable_hash


def test_stable_hash_returns_16_hex_chars() -> None:
    """`stable_hash` returns a 16-character hex digest."""
    key = stable_hash((1, 2), {"a": 1})
    assert len(key) == 16  # noqa: PLR2004
    int(key, 16)


def test_stable_hash_is_deterministic() -> None:
    """Same inputs produce the same digest."""
    a = stable_hash((1, "x"), {"k": 2})
    b = stable_hash((1, "x"), {"k": 2})
    assert a == b


def test_kwargs_are_sorted() -> None:
    """Different kwarg insertion orders produce the same digest."""
    a = stable_hash((), {"a": 1, "b": 2})
    b = stable_hash((), {"b": 2, "a": 1})
    assert a == b


def test_different_args_produce_different_digests() -> None:
    """Distinct call signatures hash to distinct keys."""
    a = stable_hash((1,), {})
    b = stable_hash((2,), {})
    assert a != b


def test_default_cache_key_format() -> None:
    r"""The default key is `f"{name}:{digest}"`."""
    key = default_cache_key("github", (1,), {"x": 2})
    name, _, digest = key.partition(":")
    assert name == "github"
    assert len(digest) == 16  # noqa: PLR2004


def test_handles_non_hashable_args() -> None:
    """Lists and dicts hash through their `repr`."""
    key = stable_hash(([1, 2, 3],), {"meta": {"k": "v"}})
    assert len(key) == 16  # noqa: PLR2004
