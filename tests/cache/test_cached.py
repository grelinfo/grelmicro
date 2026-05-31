"""Test Cached Decorator."""

import asyncio
import threading
import time
from contextlib import suppress

import pytest

from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import PickleSerializer
from grelmicro.cache.ttl import TTLCache

pytestmark = [pytest.mark.timeout(10)]

EXPECTED_DOUBLE_5 = 10
EXPECTED_CALL_COUNT_1 = 1
EXPECTED_CALL_COUNT_2 = 2
EXPECTED_HITS_1 = 1
EXPECTED_MISSES_1 = 1
EXPECTED_MISSES_2 = 2
EXPECTED_CURRSIZE_2 = 2


def _make_cache(maxsize: int = 10, ttl: float = 60) -> TTLCache:
    """Create a TTLCache with the in-memory backend and a serializer.

    Captures the running loop on the backend so the sync ``@cached``
    wrapper can dispatch from a worker thread without an explicit
    ``async with backend:`` setup. Production code opens the backend
    through the ``Grelmicro`` app, which captures the loop on entry.
    """
    backend = MemoryCacheAdapter()
    with suppress(RuntimeError):
        backend._loop = asyncio.get_running_loop()
    return TTLCache(
        maxsize=maxsize,
        ttl=ttl,
        backend=backend,
        serializer=PickleSerializer(),
    )


# ---------------------------------------------------------------------------
# Async function tests
# ---------------------------------------------------------------------------


class TestAsyncCachedBasic:
    """Test @cached decorator with async functions: basic caching behavior."""

    async def test_caches_result(self) -> None:
        """Repeated async calls return the cached result without recomputing."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        async def fetch(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        # Act
        first = await fetch(1)
        second = await fetch(1)

        # Assert
        assert first == {"id": 1}
        assert second == {"id": 1}
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_different_args_produce_different_keys(self) -> None:
        """Different arguments result in separate cache entries."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        async def fetch(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        # Act
        await fetch(1)
        await fetch(2)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_kwargs_are_part_of_key(self) -> None:
        """Kwargs are included in the cache key."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        async def greet(name: str, *, greeting: str = "hi") -> str:
            nonlocal call_count
            call_count += 1
            return f"{greeting} {name}"

        # Act
        await greet("alice", greeting="hello")
        await greet("alice", greeting="hey")

        # Assert: different kwargs means different cache keys
        assert call_count == EXPECTED_CALL_COUNT_2


class TestAsyncCachedSkip:
    """Test @cached skip predicate with async functions."""

    async def test_skip_none_results_not_cached(self) -> None:
        """Results matching skip predicate are not stored in cache."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        async def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act: None result not cached, so each call invokes function
        await maybe_fetch(found=False)
        await maybe_fetch(found=False)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_skip_does_not_affect_valid_results(self) -> None:
        """Results not matching skip predicate are cached normally."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        async def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        await maybe_fetch(found=True)
        await maybe_fetch(found=True)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1


class TestAsyncCachedTyped:
    """Test @cached with typed=True key generation on async functions."""

    async def test_typed_distinguishes_int_and_float(self) -> None:
        """typed=True caches 3 and 3.0 as separate entries."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, typed=True)
        async def compute(x: float) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act
        await compute(3)
        await compute(3.0)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_typed_distinguishes_same_repr(self) -> None:
        """typed=True separates types that share the same repr."""

        class A:
            def __repr__(self) -> str:
                return "X"

        class B:
            def __repr__(self) -> str:
                return "X"

        cache = _make_cache()
        call_count = 0

        @cached(cache, typed=True)
        async def identity(x: object) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act
        await identity(A())
        await identity(B())

        # Assert: same repr but different types produces two cache entries
        assert call_count == EXPECTED_CALL_COUNT_2


class TestAsyncCachedCacheControl:
    """Test cache_info() and cache_clear() on async decorated functions."""

    async def test_cache_info_tracks_hits_and_misses(self) -> None:
        """cache_info() returns accurate hit/miss statistics."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        # Act
        await fetch(1)  # miss
        await fetch(2)  # miss
        await fetch(1)  # hit
        info = fetch.cache_info()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Assert
        assert info.hits == EXPECTED_HITS_1
        assert info.misses == EXPECTED_MISSES_2
        assert info.currsize == EXPECTED_CURRSIZE_2

    async def test_cache_clear_is_coroutine(self) -> None:
        """cache_clear() on a TTLCache-backed function is a coroutine."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        # Assert
        assert asyncio.iscoroutinefunction(fetch.cache_clear)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    async def test_cache_clear_empties_the_cache(self) -> None:
        """Awaiting cache_clear() causes subsequent calls to recompute."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        await fetch(1)
        assert call_count == EXPECTED_CALL_COUNT_1

        # Act
        await fetch.cache_clear()  # type: ignore[attr-defined]
        await fetch(1)

        # Assert: recomputed after clear
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_cache_info_currsize_resets_after_clear(self) -> None:
        """Currsize drops to zero after clear."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        await fetch(1)
        await fetch(2)
        assert fetch.cache_info().currsize == EXPECTED_CURRSIZE_2  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Act
        await fetch.cache_clear()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Assert
        assert fetch.cache_info().currsize == 0  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]


class TestAsyncCachedFunctionMetadata:
    """Test that @cached preserves decorated async function metadata."""

    async def test_preserves_name(self) -> None:
        """The async wrapper preserves __name__."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        async def my_async_function() -> None:
            pass

        # Assert
        assert my_async_function.__name__ == "my_async_function"

    async def test_preserves_doc(self) -> None:
        """The async wrapper preserves __doc__."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        async def documented() -> None:
            """Return nothing."""

        # Assert
        assert documented.__doc__ == "Return nothing."


class TestAsyncCachedKeyMaker:
    """Test @cached with custom key_maker on async functions."""

    async def test_custom_key_maker(self) -> None:
        """Custom key_maker is used for cache keys."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, key_maker=lambda _func, args, _kwargs: str(args[0]))
        async def fetch(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        # Act
        await fetch(1)
        await fetch(1)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_custom_key_maker_isolates_entries(self) -> None:
        """Custom key_maker can collapse different args to the same key."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        # Always return the same key regardless of args
        @cached(cache, key_maker=lambda _func, _args, _kwargs: "fixed")
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        # Act: different args, but key_maker collapses them
        await fetch(1)
        await fetch(2)

        # Assert: both map to same key, only one computation
        assert call_count == EXPECTED_CALL_COUNT_1


class TestAsyncCachedLock:
    """Test @cached with lock-based stampede protection on async functions."""

    async def test_lock_true_prevents_duplicate_computation(self) -> None:
        """lock=True ensures only one coroutine computes on concurrent miss."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock=True)
        async def slow_fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            await barrier.wait()
            return x * 2

        # Act: launch two concurrent tasks hitting the same key
        task1 = asyncio.create_task(slow_fetch(5))
        task2 = asyncio.create_task(slow_fetch(5))
        await asyncio.sleep(0)  # allow both tasks to start
        barrier.set()
        result1 = await task1
        result2 = await task2

        # Assert: only one computation, both tasks receive the same result
        assert call_count == EXPECTED_CALL_COUNT_1
        assert result1 == EXPECTED_DOUBLE_5
        assert result2 == EXPECTED_DOUBLE_5

    async def test_lock_true_per_key_allows_parallel_different_keys(
        self,
    ) -> None:
        """lock=True uses per-key locks: different keys run in parallel."""
        # Arrange
        cache = _make_cache()
        order: list[str] = []
        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()

        @cached(cache, lock=True)
        async def fetch(key: str) -> str:
            if key == "a":
                order.append("a:start")
                await barrier_a.wait()
                order.append("a:end")
            else:
                order.append("b:start")
                await barrier_b.wait()
                order.append("b:end")
            return key

        # Act: launch tasks on different keys concurrently
        task_a = asyncio.create_task(fetch("a"))
        await asyncio.sleep(0)
        task_b = asyncio.create_task(fetch("b"))
        await asyncio.sleep(0)

        # Both tasks should have started (independent per-key locks)
        assert "a:start" in order
        assert "b:start" in order

        # Finish "b" while "a" is still blocked
        barrier_b.set()
        await task_b
        assert "b:end" in order
        assert "a:end" not in order

        # Now finish "a"
        barrier_a.set()
        await task_a
        assert "a:end" in order

    async def test_per_key_lock_eviction_bounds_idle_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_evict_idle_locks` trims an oversize dict down to the budget."""
        import sys  # noqa: PLC0415
        from collections import OrderedDict  # noqa: PLC0415

        import grelmicro.cache.cached  # noqa: F401, PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        monkeypatch.setattr(cached_mod, "_PER_KEY_LOCK_BUDGET", 4)
        locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        for i in range(20):
            locks[f"k{i}"] = asyncio.Lock()
            cached_mod._evict_idle_locks(locks)
        assert len(locks) == 4  # noqa: PLR2004

    async def test_per_key_lock_eviction_keeps_held_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Eviction never drops a lock that is currently held."""
        import sys  # noqa: PLC0415
        from collections import OrderedDict  # noqa: PLC0415

        import grelmicro.cache.cached  # noqa: F401, PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        monkeypatch.setattr(cached_mod, "_PER_KEY_LOCK_BUDGET", 2)
        locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        held = asyncio.Lock()
        await held.acquire()
        try:
            locks["held"] = held
            for i in range(10):
                locks[f"idle-{i}"] = asyncio.Lock()
            cached_mod._evict_idle_locks(locks)
            assert "held" in locks
        finally:
            held.release()

    def test_per_key_lock_eviction_keeps_held_sync_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sync eviction never drops a threading.Lock that is held."""
        import sys  # noqa: PLC0415
        from collections import OrderedDict  # noqa: PLC0415

        import grelmicro.cache.cached  # noqa: F401, PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        monkeypatch.setattr(cached_mod, "_PER_KEY_LOCK_BUDGET", 2)
        locks: OrderedDict[str, threading.Lock] = OrderedDict()
        held = threading.Lock()
        held.acquire()
        try:
            locks["held"] = held
            for i in range(10):
                locks[f"idle-{i}"] = threading.Lock()
            cached_mod._evict_idle_locks_sync(locks)
            assert "held" in locks
        finally:
            held.release()

    async def test_custom_asyncio_lock_prevents_duplicate_computation(
        self,
    ) -> None:
        """A custom asyncio.Lock provides global stampede protection."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock=asyncio.Lock())
        async def slow_fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            await barrier.wait()
            return x * 2

        # Act
        task1 = asyncio.create_task(slow_fetch(5))
        task2 = asyncio.create_task(slow_fetch(5))
        await asyncio.sleep(0)
        barrier.set()
        await task1
        await task2

        # Assert: global lock serializes concurrent same-key misses
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_lock_true_cache_hit_does_not_acquire_lock(self) -> None:
        """A cache hit returns immediately without touching the lock."""
        # Arrange
        cache = _make_cache()

        @cached(cache, lock=True)
        async def fetch(x: int) -> int:
            return x

        # Populate the cache
        await fetch(1)

        # Act: second call is a cache hit, no lock needed
        result = await fetch(1)

        # Assert
        assert result == 1
        assert cache.cache_info().hits == EXPECTED_HITS_1

    async def test_lock_false_disables_protection(self) -> None:
        """lock=False behaves like lock=None (no protection but still caches)."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock=False)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        # Act
        await fetch(5)
        await fetch(5)

        # Assert: still caches correctly
        assert call_count == EXPECTED_CALL_COUNT_1
        assert cache.cache_info().hits == EXPECTED_HITS_1


# ---------------------------------------------------------------------------
# Sync function tests
# ---------------------------------------------------------------------------
# Sync decorated functions dispatch onto the cache backend's captured event
# loop via asyncio.run_coroutine_threadsafe. They require a running loop, so
# the pattern is to call them from a thread launched by asyncio.to_thread
# inside an async test.


class TestSyncCachedBasic:
    """Test @cached decorator with sync functions."""

    async def test_caches_result(self) -> None:
        """Repeated sync calls return the cached result without recomputing."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act: run sync function from a thread (provides the required event loop)
        async def run() -> tuple[int, int]:
            first = await asyncio.to_thread(lambda: compute(5))
            second = await asyncio.to_thread(lambda: compute(5))
            return first, second

        first, second = await run()

        # Assert
        assert first == EXPECTED_DOUBLE_5
        assert second == EXPECTED_DOUBLE_5
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_different_args_produce_different_keys(self) -> None:
        """Different arguments result in separate cache entries."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        await asyncio.to_thread(lambda: compute(1))
        await asyncio.to_thread(lambda: compute(2))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_kwargs_are_part_of_key(self) -> None:
        """Kwargs are included in the cache key."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        def greet(name: str, *, greeting: str = "hi") -> str:
            nonlocal call_count
            call_count += 1
            return f"{greeting} {name}"

        # Act
        await asyncio.to_thread(lambda: greet("alice", greeting="hello"))
        await asyncio.to_thread(lambda: greet("alice", greeting="hey"))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2


class TestSyncCachedSkip:
    """Test @cached skip predicate with sync functions."""

    async def test_skip_none_results_not_cached(self) -> None:
        """Results matching skip predicate are not stored in cache."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        await asyncio.to_thread(lambda: maybe_fetch(found=False))
        await asyncio.to_thread(lambda: maybe_fetch(found=False))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_skip_does_not_affect_valid_results(self) -> None:
        """Results not matching skip predicate are cached normally."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        await asyncio.to_thread(lambda: maybe_fetch(found=True))
        await asyncio.to_thread(lambda: maybe_fetch(found=True))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1


class TestSyncCachedTyped:
    """Test @cached with typed=True on sync functions."""

    async def test_typed_distinguishes_int_and_float(self) -> None:
        """typed=True caches 3 and 3.0 as separate entries."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, typed=True)
        def compute(x: float) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act
        await asyncio.to_thread(lambda: compute(3))
        await asyncio.to_thread(lambda: compute(3.0))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2


class TestSyncCachedCacheControl:
    """Test cache_info() and cache_clear() on sync decorated functions."""

    async def test_cache_info_tracks_hits_and_misses(self) -> None:
        """cache_info() returns accurate hit/miss statistics."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        def compute(x: int) -> int:
            return x * 2

        # Act
        await asyncio.to_thread(lambda: compute(1))  # miss
        await asyncio.to_thread(lambda: compute(1))  # hit
        info = compute.cache_info()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Assert
        assert info.hits == EXPECTED_HITS_1
        assert info.misses == EXPECTED_MISSES_1

    async def test_cache_clear_empties_the_cache(self) -> None:
        """Awaiting cache_clear() causes subsequent sync calls to recompute."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await asyncio.to_thread(lambda: compute(1))
        assert call_count == EXPECTED_CALL_COUNT_1

        # Act: cache_clear is always a coroutine
        await compute.cache_clear()  # type: ignore[attr-defined]
        await asyncio.to_thread(lambda: compute(1))

        # Assert: recomputed after clear
        assert call_count == EXPECTED_CALL_COUNT_2


class TestSyncCachedFunctionMetadata:
    """Test that @cached preserves decorated sync function metadata."""

    async def test_preserves_name(self) -> None:
        """The sync wrapper preserves __name__."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        def my_function() -> None:
            pass

        # Assert
        assert my_function.__name__ == "my_function"

    async def test_preserves_doc(self) -> None:
        """The sync wrapper preserves __doc__."""
        # Arrange
        cache = _make_cache()

        @cached(cache)
        def documented() -> None:
            """Return nothing."""

        # Assert
        assert documented.__doc__ == "Return nothing."


class TestSyncCachedKeyMaker:
    """Test @cached with custom key_maker on sync functions."""

    async def test_custom_key_maker(self) -> None:
        """Custom key_maker is used for cache keys."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, key_maker=lambda _func, args, _kwargs: str(args[0]))
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        await asyncio.to_thread(lambda: compute(5))
        await asyncio.to_thread(lambda: compute(5))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1


class TestSyncCachedLock:
    """Test @cached with lock-based stampede protection on sync functions."""

    async def test_lock_true_prevents_duplicate_computation(self) -> None:
        """lock=True with threading.Lock prevents redundant computation."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock=True)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        await asyncio.to_thread(lambda: compute(5))
        await asyncio.to_thread(lambda: compute(5))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_lock_true_per_key_promotes_existing_sync_lock(self) -> None:
        """A repeated miss on the same key promotes its lock in LRU order."""
        # Arrange
        cache = _make_cache()

        @cached(cache, lock=True)
        def compute(x: int) -> int:
            return x * 2

        # Act: first miss creates the lock, then clear and miss again so the
        # second call hits the existing-lock branch (move_to_end).
        await asyncio.to_thread(lambda: compute(7))
        await cache.clear()
        await asyncio.to_thread(lambda: compute(7))

    async def test_custom_threading_lock_prevents_stampede(self) -> None:
        """A custom threading.Lock provides global stampede protection."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        # started is set the first time slow_compute begins executing
        started = threading.Event()

        @cached(cache, lock=threading.Lock())
        def slow_compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            started.set()
            time.sleep(0.05)
            return x * 2

        # Act: two concurrent worker threads, each calling slow_compute.
        # The cache backend captured the event loop on __aenter__, so
        # the sync wrapper inside slow_compute can dispatch onto it.
        async with asyncio.TaskGroup() as tg:
            results: list[int] = []

            async def run_one() -> None:
                result = await asyncio.to_thread(lambda: slow_compute(5))
                results.append(result)

            tg.create_task(run_one())
            # Give the first task a moment to enter slow_compute before the second starts
            await asyncio.sleep(0.01)
            tg.create_task(run_one())

        # Assert: only one computation, both callers receive the same result
        assert call_count == EXPECTED_CALL_COUNT_1
        assert sorted(results) == [EXPECTED_DOUBLE_5, EXPECTED_DOUBLE_5]

    async def test_lock_true_per_key_sync(self) -> None:
        """lock=True uses per-key threading.Lock: same key serialized, different keys not."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock=True)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act: same key, sequential via threads
        await asyncio.to_thread(lambda: compute(5))
        await asyncio.to_thread(lambda: compute(5))

        # Different key should compute independently
        await asyncio.to_thread(lambda: compute(6))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2  # 5 once, 6 once

    async def test_lock_false_still_caches(self) -> None:
        """lock=False behaves like lock=None (no protection but still caches)."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock=False)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        await asyncio.to_thread(lambda: compute(5))
        await asyncio.to_thread(lambda: compute(5))

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1
        assert cache.cache_info().hits == EXPECTED_HITS_1
