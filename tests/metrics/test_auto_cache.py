"""Auto-instrumentation tests for the cache component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.ttl import TTLCache

if TYPE_CHECKING:
    import pytest

    from tests.metrics.conftest import MetricsHarness


async def test_cache_emits_hit_and_miss(
    metrics_reader: MetricsHarness,
) -> None:
    """A miss then a hit emit cache operations with the matching result."""
    cache: TTLCache[bytes] = TTLCache(ttl=60, backend=MemoryCacheAdapter())

    assert await cache.get("k") is None  # miss
    await cache.set("k", b"v")
    assert await cache.get("k") == b"v"  # hit

    ops = metrics_reader.points("grelmicro.cache.operations")
    results = {attrs["result"] for _, attrs in ops}
    assert results == {"hit", "miss"}


async def test_cache_metrics_noop_when_off() -> None:
    """Cache reads work without error when no Metrics component is active."""
    cache: TTLCache[bytes] = TTLCache(ttl=60, backend=MemoryCacheAdapter())
    assert await cache.get("missing") is None


async def test_early_refresh_emits_both_outcomes(
    metrics_reader: MetricsHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background refresh records success and failure on one counter."""
    import asyncio  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from typing import Any  # noqa: PLC0415

    from grelmicro.cache.cached import cached  # noqa: PLC0415
    from grelmicro.cache.serializers import JsonSerializer  # noqa: PLC0415

    cached_mod = sys.modules["grelmicro.cache.cached"]
    now = 1000.0
    monkeypatch.setattr(cached_mod, "_now", lambda: now)
    monkeypatch.setattr(cached_mod, "_xfetch_should_refresh", lambda *_: True)

    backend = MemoryCacheAdapter()
    async with backend:
        cache: TTLCache[Any] = TTLCache(
            ttl=60, backend=backend, serializer=JsonSerializer()
        )
        fail = False

        @cached(cache, key="k", early=0.5)
        async def compute() -> int:
            if fail:
                msg = "upstream down"
                raise RuntimeError(msg)
            return 1

        await compute()  # cold miss, writes the refresh metadata
        now = 1040.0  # inside the early-refresh window
        await compute()  # schedules a refresh that succeeds
        await asyncio.sleep(0.05)
        fail = True
        # The successful refresh rewrote the metadata, so move back into the
        # window before the next read can schedule another refresh.
        now = 1075.0
        await compute()  # schedules a refresh that raises
        await asyncio.sleep(0.05)

    points = metrics_reader.points("grelmicro.cache.early_refreshes")
    outcomes = {attrs["outcome"] for _, attrs in points}
    assert outcomes == {"success", "error"}
    errors = [attrs for _, attrs in points if attrs["outcome"] == "error"]
    assert errors[0]["error.type"] == "RuntimeError"
