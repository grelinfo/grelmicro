"""Test Cached Decorator."""

import asyncio
import threading
import time
from contextlib import suppress
from unittest.mock import patch

import pytest

from grelmicro import Grelmicro
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer, PickleSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter

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


def _tag_keys(cache: TTLCache) -> dict[str, set[str]]:
    """Return the in-memory backend's forward tag map for assertions."""
    backend = cache._backend
    assert isinstance(backend, MemoryCacheAdapter)
    return backend._tag_keys


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
        await fetch.cache_clear()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
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


class TestAsyncCachedStampede:
    """Test @cached stampede protection on async functions."""

    async def test_lock_true_prevents_duplicate_computation(self) -> None:
        """lock='local' ensures only one coroutine computes on concurrent miss."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock="local")
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
        """lock='local' uses per-key locks: different keys run in parallel."""
        # Arrange
        cache = _make_cache()
        order: list[str] = []
        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()

        @cached(cache, lock="local")
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

        import grelmicro.cache._stampede  # noqa: F401, PLC0415

        stampede_mod = sys.modules["grelmicro.cache._stampede"]

        monkeypatch.setattr(stampede_mod, "_PER_KEY_LOCK_BUDGET", 4)
        locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        for i in range(20):
            locks[f"k{i}"] = asyncio.Lock()
            stampede_mod._evict_idle_locks(locks)
        assert len(locks) == 4  # noqa: PLR2004

    async def test_per_key_lock_eviction_trims_oversize_dict_in_one_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One call trims a dict already far over budget down to the budget.

        Adds many idle locks at once before a single `_evict_idle_locks`
        call, so a body that stops after one eviction (`break` to `return`)
        is caught.
        """
        import sys  # noqa: PLC0415
        from collections import OrderedDict  # noqa: PLC0415

        import grelmicro.cache._stampede  # noqa: F401, PLC0415

        stampede_mod = sys.modules["grelmicro.cache._stampede"]

        monkeypatch.setattr(stampede_mod, "_PER_KEY_LOCK_BUDGET", 3)
        locks: OrderedDict[str, asyncio.Lock] = OrderedDict(
            (f"k{i}", asyncio.Lock()) for i in range(10)
        )

        stampede_mod._evict_idle_locks(locks)

        assert len(locks) == 3  # noqa: PLR2004
        # Eviction drops the oldest first, so the newest keys remain.
        assert list(locks) == ["k7", "k8", "k9"]

    def test_stampede_lock_name_is_prefixed_32_char_digest(self) -> None:
        """The lock name is `cache.stampede.<first 32 hex of sha256(key)>`."""
        import hashlib  # noqa: PLC0415
        import sys  # noqa: PLC0415

        import grelmicro.cache._stampede  # noqa: F401, PLC0415

        stampede_mod = sys.modules["grelmicro.cache._stampede"]

        key = "my.module.fn:abc123"
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        expected = f"cache.stampede.{digest}"

        name = stampede_mod._stampede_lock_name(key)

        assert name == expected
        assert len(digest) == 32  # noqa: PLR2004

    async def test_per_key_lock_eviction_keeps_held_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Eviction never drops a lock that is currently held."""
        import sys  # noqa: PLC0415
        from collections import OrderedDict  # noqa: PLC0415

        import grelmicro.cache._stampede  # noqa: F401, PLC0415

        stampede_mod = sys.modules["grelmicro.cache._stampede"]

        monkeypatch.setattr(stampede_mod, "_PER_KEY_LOCK_BUDGET", 2)
        locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        held = asyncio.Lock()
        await held.acquire()
        try:
            locks["held"] = held
            for i in range(10):
                locks[f"idle-{i}"] = asyncio.Lock()
            stampede_mod._evict_idle_locks(locks)
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

    async def test_stampede_none_allows_duplicate_computation(self) -> None:
        """lock=False runs the function for every concurrent miss."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock=False)
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

        # Assert: no dedup, both concurrent misses computed
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_lock_true_cache_hit_does_not_acquire_lock(self) -> None:
        """A cache hit returns immediately without acquiring the per-key lock."""
        # Arrange
        cache = _make_cache()

        @cached(cache, lock="local")
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
        """lock=False disables protection but still caches."""
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
        await compute.cache_clear()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
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


class TestSyncCachedStampede:
    """Test @cached stampede protection on sync functions."""

    async def test_lock_true_prevents_duplicate_computation(self) -> None:
        """lock='local' with threading.Lock prevents redundant computation."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock="local")
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

        @cached(cache, lock="local")
        def compute(x: int) -> int:
            return x * 2

        # Act: first miss creates the lock, then clear and miss again so the
        # second call hits the existing-lock branch (move_to_end).
        await asyncio.to_thread(lambda: compute(7))
        await cache.clear()
        await asyncio.to_thread(lambda: compute(7))

    async def test_local_prevents_stampede_under_concurrency(self) -> None:
        """lock='local' folds concurrent same-key sync misses to one run."""
        # Arrange
        cache = _make_cache()
        call_count = 0
        # started is set the first time slow_compute begins executing
        started = threading.Event()

        @cached(cache, lock="local")
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
        """lock='local' uses per-key threading.Lock: same key serialized, different keys not."""
        # Arrange
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock="local")
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
        """lock=False disables protection but still caches."""
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


# ---------------------------------------------------------------------------
# Distributed stampede tests
# ---------------------------------------------------------------------------
# A distributed miss serializes through the Coordination component's lock. Two
# separate decorations of the SAME function share the cache key and the
# distributed lock but keep independent in-process locks, so they model
# two replicas folding onto one execution.


def _shared_cache(loop: asyncio.AbstractEventLoop) -> TTLCache:
    backend = MemoryCacheAdapter()
    backend._loop = loop
    return TTLCache(backend=backend, serializer=PickleSerializer())


class TestDistributedStampede:
    """Test @cached(lock=True) across simulated replicas."""

    async def test_two_replicas_fold_to_one_execution(self) -> None:
        """Concurrent distributed misses on the same key run once."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        call_count = 0
        barrier = asyncio.Event()

        def impl_factory():  # noqa: ANN202
            async def impl(x: int) -> int:
                nonlocal call_count
                call_count += 1
                await barrier.wait()
                return x * 2

            return impl

        impl = impl_factory()
        replica_a = cached(cache, lock=True)(impl)
        replica_b = cached(cache, lock=True)(impl)

        async with micro:
            task_a = asyncio.create_task(replica_a(5))
            task_b = asyncio.create_task(replica_b(5))
            await asyncio.sleep(0.02)  # let the first acquire the lock
            barrier.set()
            result_a = await task_a
            result_b = await task_b

        assert result_a == EXPECTED_DOUBLE_5
        assert result_b == EXPECTED_DOUBLE_5
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_distributed_sync_function_folds(self) -> None:
        """Distributed protection drives the lock from a sync worker thread."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        call_count = 0

        def impl(x: int) -> int:
            nonlocal call_count
            call_count += 1
            time.sleep(0.03)
            return x * 2

        replica_a = cached(cache, lock=True)(impl)
        replica_b = cached(cache, lock=True)(impl)

        async with micro:
            results: list[int] = []

            async def run(fn) -> None:  # noqa: ANN001
                results.append(await asyncio.to_thread(lambda: fn(5)))

            async with asyncio.TaskGroup() as tg:
                tg.create_task(run(replica_a))
                await asyncio.sleep(0.01)
                tg.create_task(run(replica_b))

        assert call_count == EXPECTED_CALL_COUNT_1
        assert sorted(results) == [EXPECTED_DOUBLE_5, EXPECTED_DOUBLE_5]


class TestLockTrueAutoSelect:
    """Test lock=True auto-selects distributed vs in-process by backend."""

    async def test_lock_true_without_backend_folds_in_process(self) -> None:
        """lock=True with no lock backend folds concurrent misses locally."""
        cache = _make_cache()
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock=True)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            await barrier.wait()
            return x * 2

        task_a = asyncio.create_task(fetch(5))
        task_b = asyncio.create_task(fetch(5))
        await asyncio.sleep(0.02)
        barrier.set()

        assert await task_a == EXPECTED_DOUBLE_5
        assert await task_b == EXPECTED_DOUBLE_5
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_lock_true_sync_without_backend_folds_in_process(
        self,
    ) -> None:
        """lock=True sync with no lock backend folds via the threading lock."""
        cache = _make_cache()
        call_count = 0

        @cached(cache, lock=True)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            time.sleep(0.03)
            return x * 2

        results: list[int] = []

        async def run() -> None:
            results.append(await asyncio.to_thread(lambda: compute(5)))

        async with asyncio.TaskGroup() as tg:
            tg.create_task(run())
            await asyncio.sleep(0.01)
            tg.create_task(run())

        assert call_count == EXPECTED_CALL_COUNT_1
        assert results == [EXPECTED_DOUBLE_5, EXPECTED_DOUBLE_5]


# ---------------------------------------------------------------------------
# Early (XFetch) refresh tests
# ---------------------------------------------------------------------------


class _Clock:
    """Mutable wall clock standing in for ``cached._now``."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class TestEarlyRefresh:
    """Test @cached(early=...) probabilistic XFetch refresh."""

    def test_xfetch_should_refresh_handles_zero_random(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 0.0 random draw never crashes the early-refresh die.

        ``random.random`` can return exactly 0.0, and ``math.log(0.0)``
        raises ``ValueError``. The die must clamp the draw and return a bool.
        """
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]
        monkeypatch.setattr(cached_mod, "_random", lambda: 0.0)

        # Act / Assert: no exception, a plain bool result.
        result = cached_mod._xfetch_should_refresh(remaining=10.0, delta=1.0)
        assert isinstance(result, bool)

    async def test_invalid_early_rejected(self) -> None:
        """Early outside [0, 1) raises at decoration time."""
        cache = _make_cache()
        with pytest.raises(ValueError, match="early"):
            cached(cache, early=1.0)
        with pytest.raises(ValueError, match="early"):
            cached(cache, early=-0.1)

    async def test_invalid_lock_rejected(self) -> None:
        """An unknown lock value raises at decoration time."""
        cache = _make_cache()
        with pytest.raises(ValueError, match="lock"):
            cached(cache, lock="global")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    async def test_early_outside_window_does_not_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh entry read early in its TTL is not refreshed."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        call_count = 0

        @cached(cache, early=0.5)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await fetch(5)  # miss, writes meta at t=1000
        clock.t = 1010  # remaining 50s > 30s window
        await fetch(5)  # hit, not due
        await asyncio.sleep(0.02)
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_early_in_window_schedules_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry inside the early window refreshes when the die rolls true."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        call_count = 0

        @cached(cache, early=0.5)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await fetch(5)  # miss at t=1000
        clock.t = 1040  # remaining 20s <= 30s window
        result = await fetch(5)  # hit, schedules background refresh
        assert result == EXPECTED_DOUBLE_5
        # Wait for the background refresh task to recompute.
        for _ in range(50):
            await asyncio.sleep(0.005)
            if call_count == EXPECTED_CALL_COUNT_2:
                break
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_early_die_rolls_false_no_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In the window but the die rolls false: no refresh."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: False
        )
        cache = _make_cache(ttl=60)
        call_count = 0

        @cached(cache, early=0.5)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await fetch(5)
        clock.t = 1040
        await fetch(5)
        await asyncio.sleep(0.02)
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_early_no_meta_no_refresh(self) -> None:
        """Early hit with no stored metadata falls through to normal expiry."""
        cache = _make_cache(ttl=60)
        call_count = 0

        from grelmicro.cache.cached import _make_key  # noqa: PLC0415

        async def impl(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        fetch = cached(cache, early=0.5)(impl)
        # Seed the value directly so no XFetch meta exists for the key.
        key = _make_key(impl, (5,), {}, None, typed=False)
        await cache.set(key, 10)
        await fetch(5)  # hit, meta is None
        await asyncio.sleep(0.02)
        assert call_count == 0

    async def test_early_sync_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync entry in the early window refreshes on a daemon thread."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        call_count = 0

        @cached(cache, early=0.5)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await asyncio.to_thread(lambda: compute(5))  # miss at t=1000
        clock.t = 1040
        await asyncio.to_thread(lambda: compute(5))  # hit, schedules refresh
        for _ in range(50):
            await asyncio.sleep(0.005)
            if call_count == EXPECTED_CALL_COUNT_2:
                break
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_xfetch_formula(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Vattani die fires once delta*-ln(rand) crosses remaining."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]

        # -ln(rand) ~ 0 -> never refresh, even with a costly delta.
        monkeypatch.setattr(cached_mod, "_random", lambda: 1.0)
        assert (
            cached_mod._xfetch_should_refresh(remaining=1.0, delta=10.0)
            is False
        )
        # A large -ln(rand) and a costly delta clear a small remaining.
        monkeypatch.setattr(cached_mod, "_random", lambda: 0.01)  # -ln ~ 4.6
        assert (
            cached_mod._xfetch_should_refresh(remaining=1.0, delta=10.0) is True
        )

    async def test_early_refresh_skipped_when_lock_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read does not start a second refresh while one is in flight."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        gate = asyncio.Event()
        call_count = 0

        @cached(cache, early=0.5)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            if call_count >= EXPECTED_CALL_COUNT_2:  # the refresh recompute
                await gate.wait()
            return x * 2

        await fetch(5)  # miss at t=1000
        clock.t = 1040  # in window
        await fetch(5)  # hit -> schedules refresh that holds the lock
        await asyncio.sleep(0.02)  # let the refresh task acquire the lock
        await fetch(5)  # hit -> refresh skipped, lock already held
        gate.set()
        await asyncio.sleep(0.02)
        assert call_count == EXPECTED_CALL_COUNT_2  # only one refresh ran

    async def test_early_sync_outside_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync hit early in the TTL is not refreshed."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        cache = _make_cache(ttl=60)
        call_count = 0

        @cached(cache, early=0.5)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await asyncio.to_thread(lambda: compute(5))  # miss at t=1000
        clock.t = 1010  # remaining 50s > 30s window
        await asyncio.to_thread(lambda: compute(5))  # hit, not due
        await asyncio.sleep(0.02)
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_distributed_sync_early_writes_meta(self) -> None:
        """A sync distributed cold miss with early= stores XFetch metadata."""
        from grelmicro.cache.cached import (  # noqa: PLC0415
            _make_key,
            _read_meta,
        )

        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

        def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, lock=True, early=0.5)(impl)
        async with micro:
            await asyncio.to_thread(lambda: fetch(5))
            key = _make_key(impl, (5,), {}, None, typed=False)
            meta = await _read_meta(cache, key)
        assert meta is not None

    async def test_distributed_sync_skip_not_cached(self) -> None:
        """A sync distributed miss whose result is skipped is not cached."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        call_count = 0

        def impl(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        fetch = cached(cache, lock=True, skip=lambda _: True)(impl)
        async with micro:
            await asyncio.to_thread(lambda: fetch(5))
            await asyncio.to_thread(lambda: fetch(5))
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_early_sync_refresh_skipped_when_lock_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync read does not start a second refresh while one is in flight."""
        import sys  # noqa: PLC0415

        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        gate = threading.Event()
        call_count = 0

        @cached(cache, early=0.5)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            if call_count >= EXPECTED_CALL_COUNT_2:  # the refresh recompute
                gate.wait()
            return x * 2

        await asyncio.to_thread(lambda: compute(5))  # miss at t=1000
        clock.t = 1040  # in window
        await asyncio.to_thread(lambda: compute(5))  # hit -> refresh holds lock
        await asyncio.sleep(0.05)  # let the refresh thread acquire the lock
        await asyncio.to_thread(lambda: compute(5))  # hit -> refresh skipped
        gate.set()
        await asyncio.sleep(0.05)
        assert call_count == EXPECTED_CALL_COUNT_2  # only one refresh ran


class TestCachedTags:
    """Tests for @cached tag templating."""

    async def test_literal_tags_async(self) -> None:
        """Test that literal tags are stored unchanged for an async func."""
        cache = _make_cache()

        @cached(cache, tags=["users"])
        async def fetch(user_id: int) -> dict:
            return {"id": user_id}

        await fetch(1)

        assert _tag_keys(cache)["users"]

    async def test_templated_tag_from_positional_arg(self) -> None:
        """Test that a templated tag renders from a positional argument."""
        cache = _make_cache()

        @cached(cache, tags=["user:{user_id}"])
        async def fetch(user_id: int) -> dict:
            return {"id": user_id}

        await fetch(42)

        assert "user:42" in _tag_keys(cache)

    async def test_templated_tag_from_keyword_arg(self) -> None:
        """Test that a templated tag renders from a keyword argument."""
        cache = _make_cache()

        @cached(cache, tags=["user:{user_id}"])
        async def fetch(user_id: int) -> dict:
            return {"id": user_id}

        await fetch(user_id=7)

        assert "user:7" in _tag_keys(cache)

    async def test_mixed_literal_and_templated_tags(self) -> None:
        """Test that literal and templated tags are both applied."""
        cache = _make_cache()

        @cached(cache, tags=["users", "user:{user_id}"])
        async def fetch(user_id: int) -> dict:
            return {"id": user_id}

        await fetch(3)

        assert "users" in _tag_keys(cache)
        assert "user:3" in _tag_keys(cache)

    async def test_tag_renders_from_default_argument(self) -> None:
        """Test that a templated tag uses a default when the arg is omitted."""
        cache = _make_cache()

        @cached(cache, tags=["page:{page}"])
        async def fetch(page: int = 1) -> dict:
            return {"page": page}

        await fetch()

        assert "page:1" in _tag_keys(cache)

    async def test_delete_tags_invalidates_cached_async(self) -> None:
        """Test that delete_tags drops a cached entry by tag."""
        cache = _make_cache()
        calls = 0

        @cached(cache, tags=["user:{user_id}"])
        async def fetch(user_id: int) -> dict:
            nonlocal calls
            calls += 1
            return {"id": user_id, "calls": calls}

        first = await fetch(1)
        await cache.delete_tags("user:1")
        second = await fetch(1)

        assert first["calls"] == 1
        assert second["calls"] == 2  # noqa: PLR2004

    def test_literal_tags_sync(self) -> None:
        """Test that tags are applied for a sync cached function."""
        backend = MemoryCacheAdapter()
        cache = TTLCache(ttl=60, backend=backend, serializer=PickleSerializer())

        @cached(cache, tags=["user:{user_id}"])
        def fetch(user_id: int) -> dict:
            return {"id": user_id}

        async def run() -> None:
            backend._loop = asyncio.get_running_loop()
            await asyncio.to_thread(fetch, 5)

        asyncio.run(run())

        assert "user:5" in backend._tag_keys


class TestCachedStaleOnError:
    """Test @cached(stale_ttl=...) serve-stale-on-error."""

    def _cache(self) -> TTLCache:
        return TTLCache(
            ttl=5, backend=MemoryCacheAdapter(), serializer=JsonSerializer()
        )

    async def test_invalid_stale_ttl_rejected(self) -> None:
        """A non-positive stale_ttl is rejected at decoration time."""
        cache = self._cache()
        with pytest.raises(ValueError, match="stale_ttl"):
            cached(cache, stale_ttl=0)
        with pytest.raises(ValueError, match="stale_ttl"):
            cached(cache, stale_ttl=-1)

    async def test_serves_stale_when_recompute_fails(self) -> None:
        """After the TTL, a failing recompute serves the last good value."""
        cache = self._cache()
        fail = False
        calls = 0

        @cached(cache, stale_ttl=100)
        async def fetch() -> int:
            nonlocal calls
            calls += 1
            if fail:
                msg = "down"
                raise RuntimeError(msg)
            return calls

        now = time.monotonic()
        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            assert await fetch() == EXPECTED_CALL_COUNT_1

        fail = True
        # Primary entry expired (ttl=5), stale reserve alive (ttl=105).
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 10):
            assert await fetch() == EXPECTED_CALL_COUNT_1

    async def test_propagates_when_reserve_also_expired(self) -> None:
        """Past the stale window the original error propagates."""
        cache = self._cache()
        fail = False

        @cached(cache, stale_ttl=100)
        async def fetch() -> int:
            if fail:
                msg = "down"
                raise RuntimeError(msg)
            return 1

        now = time.monotonic()
        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            assert await fetch() == EXPECTED_CALL_COUNT_1

        fail = True
        with (
            patch("grelmicro.cache.memory.monotonic", return_value=now + 200),
            pytest.raises(RuntimeError, match="down"),
        ):
            await fetch()

    async def test_no_stale_ttl_propagates_error(self) -> None:
        """Without stale_ttl, a failing recompute propagates as usual."""
        cache = self._cache()
        fail = False

        @cached(cache)
        async def fetch() -> int:
            if fail:
                msg = "down"
                raise RuntimeError(msg)
            return 1

        now = time.monotonic()
        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            assert await fetch() == EXPECTED_CALL_COUNT_1

        fail = True
        with (
            patch("grelmicro.cache.memory.monotonic", return_value=now + 10),
            pytest.raises(RuntimeError, match="down"),
        ):
            await fetch()

    async def test_fresh_hit_never_recomputes(self) -> None:
        """A within-TTL hit never reaches the recompute or stale path."""
        cache = self._cache()
        calls = 0

        @cached(cache, stale_ttl=100)
        async def fetch() -> int:
            nonlocal calls
            calls += 1
            return calls

        assert await fetch() == EXPECTED_CALL_COUNT_1
        assert await fetch() == EXPECTED_CALL_COUNT_1
        assert calls == EXPECTED_CALL_COUNT_1


class TestSyncCachedStaleOnError:
    """Test sync @cached(stale_ttl=...) serve-stale-on-error."""

    async def test_serves_stale_when_recompute_fails(self) -> None:
        """A failing sync recompute serves the last good value."""
        cache = _make_cache(ttl=5)
        fail = False
        calls = 0

        @cached(cache, stale_ttl=100)
        def fetch() -> int:
            nonlocal calls
            calls += 1
            if fail:
                msg = "down"
                raise RuntimeError(msg)
            return calls

        now = time.monotonic()
        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            first = await asyncio.to_thread(fetch)
        assert first == EXPECTED_CALL_COUNT_1

        fail = True
        # Primary entry expired (ttl=5), stale reserve alive (ttl=105).
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 10):
            served = await asyncio.to_thread(fetch)
        assert served == EXPECTED_CALL_COUNT_1

    async def test_propagates_when_no_reserve(self) -> None:
        """A cold sync failure with no stored value propagates the error."""
        cache = _make_cache(ttl=5)

        @cached(cache, stale_ttl=100)
        def fetch() -> int:
            msg = "down"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="down"):
            await asyncio.to_thread(fetch)


class TestSyncCachedNoLoop:
    """Test the error raised when the sync wrapper is called without an open backend."""

    def test_raises_runtime_error_when_loop_is_none(self) -> None:
        """Calling a sync cached function before the backend is opened raises RuntimeError."""
        backend = MemoryCacheAdapter()
        cache = TTLCache(
            maxsize=10,
            ttl=60,
            backend=backend,
            serializer=PickleSerializer(),
        )

        @cached(cache)
        def compute(x: int) -> int:
            return x * 2

        with pytest.raises(RuntimeError, match="async with"):
            compute(5)


# ---------------------------------------------------------------------------
# Zero-object @cached(ttl=...) private-cache form
# ---------------------------------------------------------------------------


class TestPrivateCacheForm:
    """Test @cached(ttl=...) building a private process-local cache."""

    async def test_async_memoizes_with_ttl(self) -> None:
        """@cached(ttl=...) on an async function memoizes results."""
        call_count = 0

        @cached(ttl=30)
        async def get_rates() -> dict:
            nonlocal call_count
            call_count += 1
            return {"usd": 1.0}

        assert await get_rates() == {"usd": 1.0}
        assert await get_rates() == {"usd": 1.0}
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_private_cache_exposes_helpers(self) -> None:
        """The private form still exposes cache_info and cache_clear."""
        expected_value = 7

        @cached(ttl=30)
        async def get_value() -> int:
            return expected_value

        assert await get_value() == expected_value
        info = get_value.cache_info()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        assert info.hits == 0
        assert info.misses == EXPECTED_MISSES_1
        await get_value.cache_clear()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    async def test_maxsize_bounds_private_cache(self) -> None:
        """maxsize= bounds the private cache and evicts the oldest entry."""
        call_count = 0
        expected_calls = 3

        @cached(ttl=30, maxsize=1)
        async def square(n: int) -> int:
            nonlocal call_count
            call_count += 1
            return n * n

        assert await square(2) == 2 * 2
        assert await square(3) == 3 * 3
        # The first entry was evicted, so recomputing it counts a third call.
        assert await square(2) == 2 * 2
        assert call_count == expected_calls

    def test_both_cache_and_ttl_raises(self) -> None:
        """Passing both cache and ttl raises TypeError."""
        with pytest.raises(TypeError, match="not both"):

            @cached(TTLCache(ttl=5), ttl=5)
            async def f() -> int:
                return 1

    def test_neither_cache_nor_ttl_raises(self) -> None:
        """Bare @cached() with neither cache nor ttl raises TypeError."""
        with pytest.raises(TypeError, match="needs a cache or a ttl="):

            @cached()
            async def f() -> int:
                return 1


def test_private_cache_rejects_sync_function() -> None:
    """The zero-object form raises at decoration on a sync function."""
    with pytest.raises(TypeError, match="async functions only"):

        @cached(ttl=30)
        def sync_fn() -> int:
            return 1
