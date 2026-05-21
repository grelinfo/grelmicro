"""Cache and fallback chain tests on Shield give-up."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grelmicro.resilience import Shield


async def _flush_pending_tasks() -> None:
    """Yield enough times for fire-and-forget tasks to complete."""
    # `asyncio.sleep` is patched to a no-op in conftest, so we drain
    # the loop by polling outstanding tasks directly.
    for _ in range(20):
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


class _SignalError(Exception):
    """Test-only retryable error."""


class _SpyCache:
    """In-memory cache exposing `get(key)` and `set(key, value)`."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any]] = []
        self.get_calls: list[str] = []

    async def get(self, key: str) -> Any:  # noqa: ANN401
        self.get_calls.append(key)
        return self.data.get(key)

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        self.set_calls.append((key, value))
        self.data[key] = value


def _shield(**overrides: Any) -> Shield:  # noqa: ANN401
    return Shield.api("cached", timeout_errors=(_SignalError,), **overrides)


async def test_cache_set_fires_on_success() -> None:
    """A successful call writes the return value to the cache."""
    cache = _SpyCache()
    s = _shield(cache=cache)

    async def fn(x: int) -> int:
        return x * 2

    assert await s.run(fn, 3) == 6  # noqa: PLR2004
    # The cache write is fire-and-forget; give the loop a tick to run it.
    await _flush_pending_tasks()
    assert len(cache.set_calls) == 1
    key, value = cache.set_calls[0]
    assert key.startswith("cached:")
    assert value == 6  # noqa: PLR2004


async def test_give_up_returns_cached_value_on_hit() -> None:
    """A give-up with a cache hit returns the cached value, no exception."""
    cache = _SpyCache()
    s = _shield(cache=cache)
    # Pre-populate the cache with the key the next call will compute.
    key = s._compute_key((), {})
    cache.data[key] = "from-cache"

    async def always_fails() -> None:
        raise _SignalError

    assert await s.run(always_fails) == "from-cache"


async def test_give_up_falls_through_on_cache_miss() -> None:
    """A miss continues to the fallback or re-raises."""
    cache = _SpyCache()
    s = _shield(cache=cache)

    async def always_fails() -> None:
        raise _SignalError

    with pytest.raises(_SignalError):
        await s.run(always_fails)


async def test_fallback_only_returns_synthesized_value() -> None:
    """With no cache, the fallback callable supplies the give-up value."""

    async def synth(exc: BaseException) -> str:
        assert isinstance(exc, _SignalError)
        return "default"

    s = _shield(fallback=synth)

    async def always_fails() -> None:
        raise _SignalError

    assert await s.run(always_fails) == "default"


async def test_sync_fallback_is_supported() -> None:
    """A plain `def` fallback works without `await`."""

    def synth(_exc: BaseException) -> int:
        return 42

    s = _shield(fallback=synth)

    async def always_fails() -> None:
        raise _SignalError

    assert await s.run(always_fails) == 42  # noqa: PLR2004


async def test_cache_then_fallback_order() -> None:
    """Cache hit wins over fallback. Cache miss falls to fallback."""
    cache = _SpyCache()

    async def synth(_exc: BaseException) -> str:
        return "synthesized"

    s = _shield(cache=cache, fallback=synth)

    # Cache miss -> fallback used.
    async def always_fails() -> None:
        raise _SignalError

    assert await s.run(always_fails) == "synthesized"

    # Pre-populate cache, retry: cache hit beats fallback.
    key = s._compute_key((), {})
    cache.data[key] = "cached-wins"
    assert await s.run(always_fails) == "cached-wins"


async def test_cache_key_callable_override() -> None:
    """A user `cache_key` callable controls the lookup key."""
    cache = _SpyCache()

    def key_fn(*args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        return f"static:{args[0]}"

    s = _shield(cache=cache, cache_key=key_fn)

    async def fn(symbol: str) -> str:
        return f"value-{symbol}"

    await s.run(fn, "AAPL")
    await _flush_pending_tasks()
    keys = [call[0] for call in cache.set_calls]
    assert "static:AAPL" in keys


async def test_cache_set_failure_is_swallowed() -> None:
    """A failing cache write does not propagate."""

    class _BadCache:
        async def get(self, _key: str) -> Any:  # noqa: ANN401
            return None

        async def set(self, _key: str, _value: Any) -> None:  # noqa: ANN401
            msg = "nope"
            raise RuntimeError(msg)

    s = _shield(cache=_BadCache())

    async def fn() -> str:
        return "ok"

    assert await s.run(fn) == "ok"
    await _flush_pending_tasks()


async def test_cache_get_failure_falls_through() -> None:
    """A failing cache read is logged and falls through to the next path."""

    class _BadCache:
        async def get(self, _key: str) -> Any:  # noqa: ANN401
            msg = "boom"
            raise RuntimeError(msg)

        async def set(self, _key: str, _value: Any) -> None:  # noqa: ANN401
            return None

    async def synth(_exc: BaseException) -> str:
        return "fallback"

    s = _shield(cache=_BadCache(), fallback=synth)

    async def always_fails() -> None:
        raise _SignalError

    assert await s.run(always_fails) == "fallback"
