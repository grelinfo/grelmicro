"""Mutation-killing tests for `TTLCache` logic.

These pin behavior that the broader suite asserted too loosely: the default
TTL value, the `maxsize > 0` LRU guard, per-call TTL forwarding, stale-reserve
tagging, stat accumulation, and folding in the stale branch. Each test asserts
exact values with a nonzero base TTL and non-unit overrides so sign and
operator mutants diverge.
"""

from __future__ import annotations

import asyncio

import pytest

from grelmicro import Grelmicro
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer, PickleSerializer
from grelmicro.cache.ttl import _CACHE_PREFIX, _STALE_SUFFIX, TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter

pytestmark = [pytest.mark.timeout(10)]

_BASE_TTL = 60.0
_OVERRIDE_TTL = 25.0
_STALE_TTL = 40.0


@pytest.fixture
def backend() -> MemoryCacheAdapter:
    """Provide an isolated in-memory cache backend."""
    return MemoryCacheAdapter()


def test_default_ttl_is_sixty() -> None:
    """The unset TTL default is exactly 60 seconds."""
    cache = TTLCache()
    assert cache.config.ttl == 60.0  # noqa: PLR2004


class TestMaxsizeGuard:
    """Pin the `maxsize > 0` guard against `>= 0` and `> 1` mutants."""

    async def test_maxsize_zero_never_tracks_keys_on_hit(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`maxsize=0` skips LRU tracking, so a hit leaves currsize at 0."""
        cache = TTLCache(maxsize=0, ttl=_BASE_TTL, backend=backend)
        await cache.set("k", b"v")
        assert await cache.get("k") == b"v"
        assert cache.cache_info().currsize == 0

    async def test_maxsize_one_tracks_key_on_hit(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`maxsize=1` tracks the key, so currsize becomes 1 after a hit."""
        cache = TTLCache(maxsize=1, ttl=_BASE_TTL, backend=backend)
        await cache.set("k", b"v")
        assert await cache.get("k") == b"v"
        assert cache.cache_info().currsize == 1

    async def test_get_many_maxsize_zero_keeps_currsize_zero(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`get_many` under `maxsize=0` does not start LRU tracking."""
        cache = TTLCache(
            maxsize=0,
            ttl=_BASE_TTL,
            backend=backend,
            serializer=JsonSerializer(),
        )
        await cache.set("a", {"v": 1})
        await cache.get_many(["a"])
        assert cache.cache_info().currsize == 0

    async def test_get_many_promotes_the_found_key_not_none(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`get_many` promotes the actual found key, keeping LRU correct.

        With `maxsize=2`, reading "a" then inserting a third key must evict
        "b" (the untouched key), not "a".
        """
        cache = TTLCache(
            maxsize=2,
            ttl=_BASE_TTL,
            backend=backend,
            serializer=JsonSerializer(),
        )
        await cache.set("a", {"v": 1})
        await cache.set("b", {"v": 2})

        await cache.get_many(["a"])  # promote "a" to most-recent
        await cache.set("c", {"v": 3})  # evicts the LRU key

        assert await cache.get("a") == {"v": 1}
        assert await cache.get("b") is None
        assert await cache.get("c") == {"v": 3}


class TestPerCallTTLForwarding:
    """Pin that `get_or_set` forwards its `ttl` override to `set`."""

    async def test_get_or_set_stores_with_override_ttl(
        self,
        backend: MemoryCacheAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `ttl=` override reaches the backend, expiring the entry early.

        With base TTL 60 and override 25, the entry is gone at 30 seconds
        but would still be present if the override were dropped or nulled.
        """
        import grelmicro.cache.memory as memory_module  # noqa: PLC0415

        cache = TTLCache(
            ttl=_BASE_TTL, backend=backend, serializer=JsonSerializer()
        )
        now = 1000.0
        clock = [now]
        monkeypatch.setattr(memory_module, "monotonic", lambda: clock[0])

        await cache.get_or_set("k", lambda: {"v": 1}, ttl=_OVERRIDE_TTL)
        clock[0] = now + 30.0  # past the 25s override, before 60s base
        assert await cache.get("k") is None


class TestStaleReserveTags:
    """Pin that the stale reserve carries the value's tags."""

    async def test_set_tags_the_stale_reserve(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """The stale sidecar key is tagged alongside the value."""
        cache = TTLCache(
            ttl=_BASE_TTL, backend=backend, serializer=JsonSerializer()
        )

        await cache.set("k", {"v": 1}, tags=["g"], stale_ttl=_STALE_TTL)

        stale_key = f"{_CACHE_PREFIX}:k{_STALE_SUFFIX}"
        assert backend._tag_keys["g"] == {f"{_CACHE_PREFIX}:k", stale_key}


class TestStatAccumulation:
    """Pin that hit and eviction counters accumulate, not reset to 1."""

    async def test_get_many_accumulates_hits(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Two found keys record two hits, not one."""
        cache = TTLCache(
            ttl=_BASE_TTL, backend=backend, serializer=JsonSerializer()
        )
        await cache.set("a", {"v": 1})
        await cache.set("b", {"v": 2})

        await cache.get_many(["a", "b"])

        assert cache.cache_info().hits == 2  # noqa: PLR2004

    async def test_evictions_accumulate(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Two evictions count as two, not reset to one each time."""
        cache = TTLCache(maxsize=1, ttl=_BASE_TTL, backend=backend)
        await cache.set("a", b"a")
        await cache.set("b", b"b")  # evicts "a"
        await cache.set("c", b"c")  # evicts "b"

        assert cache.cache_info().evictions == 2  # noqa: PLR2004


class TestSetManyBoundaries:
    """Pin `set_many` TTL validation and the maxsize guard."""

    async def test_set_many_ttl_one_is_accepted(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`ttl=1` is valid (the guard rejects `<= 0`, not `<= 1`)."""
        cache = TTLCache(
            ttl=_BASE_TTL, backend=backend, serializer=JsonSerializer()
        )

        await cache.set_many({"a": {"v": 1}}, ttl=1)

        assert await cache.get("a") == {"v": 1}

    async def test_set_many_maxsize_one_evicts(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """`maxsize=1` (the `> 0` guard) evicts when set_many overflows."""
        cache = TTLCache(maxsize=1, ttl=_BASE_TTL, backend=backend)
        await cache.set("old", b"old")

        await cache.set_many({"new": b"new"})

        assert await cache.get("old") is None
        assert await cache.get("new") == b"new"
        assert cache.cache_info().currsize == 1


class TestDeleteManyUntracked:
    """Pin that delete_many tolerates keys absent from the LRU tracker."""

    async def test_delete_many_with_untracked_key_does_not_raise(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """A key never tracked locally is popped with a default, not raised."""
        cache = TTLCache(maxsize=10, ttl=_BASE_TTL, backend=backend)
        await cache.set("a", b"a")

        # "ghost" was never set, so it is absent from the LRU tracker.
        await cache.delete_many(["a", "ghost"])

        assert await cache.get("a") is None


class TestStaleBranchFolding:
    """Pin folding in the `stale_ttl is not None` branch of get_or_set."""

    async def test_stale_branch_folds_concurrent_misses(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """With `stale_ttl` set, concurrent misses still fold to one call.

        Exercises the second `compute_with_stampede` call (the try branch),
        so a `per_key=False` mutation there is caught.
        """
        cache = TTLCache(
            ttl=_BASE_TTL, backend=backend, serializer=JsonSerializer()
        )
        calls = 0
        barrier = asyncio.Event()

        async def factory() -> dict:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return {"v": calls}

        task_a = asyncio.create_task(
            cache.get_or_set("k", factory, stale_ttl=_STALE_TTL)
        )
        task_b = asyncio.create_task(
            cache.get_or_set("k", factory, stale_ttl=_STALE_TTL)
        )
        await asyncio.sleep(0.05)
        barrier.set()

        assert await task_a == {"v": 1}
        assert await task_b == {"v": 1}
        assert calls == 1


class TestDistributedGetOrSet:
    """Pin `auto_distributed=True` folding across simulated replicas."""

    async def test_two_caches_fold_across_the_distributed_lock(self) -> None:
        """Two caches sharing a backend fold concurrent misses to one call.

        Each cache has its own in-process guard, so only the cross-replica
        `Lock` (driven by `auto_distributed=True`) can fold them. A mutation
        to `auto_distributed=False` lets both compute.
        """
        loop = asyncio.get_running_loop()
        shared_backend = MemoryCacheAdapter()
        shared_backend._loop = loop
        cache_a = TTLCache(
            backend=shared_backend, serializer=PickleSerializer()
        )
        cache_b = TTLCache(
            backend=shared_backend, serializer=PickleSerializer()
        )
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0
        barrier = asyncio.Event()

        async def factory() -> int:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return calls * 10

        async with micro:
            task_a = asyncio.create_task(cache_a.get_or_set("k", factory))
            task_b = asyncio.create_task(cache_b.get_or_set("k", factory))
            await asyncio.sleep(0.05)
            barrier.set()
            result_a = await task_a
            result_b = await task_b

        assert calls == 1
        assert result_a == 10  # noqa: PLR2004
        assert result_b == 10  # noqa: PLR2004

    async def test_distributed_stale_branch_folds(self) -> None:
        """The stale branch also folds across replicas via the shared lock.

        Pins `auto_distributed=True` inside the `stale_ttl is not None`
        try branch.
        """
        loop = asyncio.get_running_loop()
        shared_backend = MemoryCacheAdapter()
        shared_backend._loop = loop
        cache_a = TTLCache(
            backend=shared_backend, serializer=PickleSerializer()
        )
        cache_b = TTLCache(
            backend=shared_backend, serializer=PickleSerializer()
        )
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0
        barrier = asyncio.Event()

        async def factory() -> int:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return calls * 10

        async with micro:
            task_a = asyncio.create_task(
                cache_a.get_or_set("k", factory, stale_ttl=_STALE_TTL)
            )
            task_b = asyncio.create_task(
                cache_b.get_or_set("k", factory, stale_ttl=_STALE_TTL)
            )
            await asyncio.sleep(0.05)
            barrier.set()
            result_a = await task_a
            result_b = await task_b

        assert calls == 1
        assert result_a == 10  # noqa: PLR2004
        assert result_b == 10  # noqa: PLR2004
