"""Auto-instrumentation tests for the cache component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.ttl import TTLCache

if TYPE_CHECKING:
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
