"""Test Cached Decorator with CacheBackend backend."""

import asyncio
import json
from typing import Any

import pytest

from grelmicro.cache.cached import cached
from grelmicro.cache.ttl import CacheInfo

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

EXPECTED_DOUBLE_5 = 10
EXPECTED_CALL_COUNT_1 = 1
EXPECTED_CALL_COUNT_2 = 2
EXPECTED_HITS_1 = 1
EXPECTED_MISSES_2 = 2
EXPECTED_CURRSIZE_2 = 2


class MockCacheBackend:
    """In-memory async cache implementing the CacheBackend protocol."""

    def __init__(self) -> None:
        """Initialize the cache."""
        self._store: dict[str, Any] = {}
        self._hits = 0
        self._misses = 0

    async def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        """Get a value by key, returning default if absent."""
        if key in self._store:
            self._hits += 1
            return self._store[key]
        self._misses += 1
        return default

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Store a value under key."""
        self._store[key] = value

    async def clear(self) -> None:
        """Remove all entries from the cache."""
        self._store.clear()

    def cache_info(self) -> CacheInfo:
        """Return a snapshot of cache statistics."""
        return CacheInfo(
            hits=self._hits,
            misses=self._misses,
            maxsize=0,
            currsize=len(self._store),
            evictions=0,
        )


class TestCacheBackendBasic:
    """Test @cached with CacheBackend on async functions: basic caching behavior."""

    async def test_caches_result(self) -> None:
        """Test that repeated calls return the cached result without recomputing."""
        # Arrange
        cache = MockCacheBackend()
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
        """Test that different arguments result in separate cache entries."""
        # Arrange
        cache = MockCacheBackend()
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
        """Test that kwargs are included in the cache key."""
        # Arrange
        cache = MockCacheBackend()
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


class TestCacheBackendSerializer:
    """Test @cached with CacheBackend and serializer/deserializer round-trip."""

    async def test_serializer_deserializer_round_trip(self) -> None:
        """Test that values are serialized before storing and deserialized on retrieval."""
        # Arrange
        cache = MockCacheBackend()

        @cached(
            cache,
            serializer=lambda v: json.dumps(v).encode(),
            deserializer=json.loads,
        )
        async def get_data() -> dict:
            return {"key": "value"}

        # Act
        first = await get_data()
        second = await get_data()

        # Assert: both calls return the deserialized dict
        assert first == {"key": "value"}
        assert second == {"key": "value"}

    async def test_serialized_value_stored_in_cache(self) -> None:
        """Test that the raw stored value is in serialized form."""
        # Arrange
        cache = MockCacheBackend()

        @cached(
            cache,
            serializer=lambda v: json.dumps(v).encode(),
            deserializer=json.loads,
        )
        async def get_data() -> dict:
            return {"key": "value"}

        await get_data()

        # Assert: the underlying store holds bytes, not the original dict
        stored_values = list(cache._store.values())
        assert len(stored_values) == 1
        assert isinstance(stored_values[0], bytes)


class TestCacheBackendSkip:
    """Test @cached with CacheBackend and a skip predicate."""

    async def test_skip_none_results_not_cached(self) -> None:
        """Test that results matching the skip predicate are not stored in cache."""
        # Arrange
        cache = MockCacheBackend()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        async def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act: None result should not be cached, so each call invokes function
        await maybe_fetch(found=False)
        await maybe_fetch(found=False)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_skip_does_not_affect_valid_results(self) -> None:
        """Test that results not matching skip predicate are cached normally."""
        # Arrange
        cache = MockCacheBackend()
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        async def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act: valid result is cached, second call is a hit
        await maybe_fetch(found=True)
        await maybe_fetch(found=True)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_1


class TestCacheBackendTyped:
    """Test @cached with CacheBackend and typed=True key generation."""

    async def test_typed_distinguishes_int_and_float(self) -> None:
        """Test that typed=True caches 3 and 3.0 as separate entries."""
        # Arrange
        cache = MockCacheBackend()
        call_count = 0

        @cached(cache, typed=True)
        async def compute(x: float) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act
        await compute(3)
        await compute(3.0)

        # Assert: typed ensures separate cache entries for int vs float
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_typed_distinguishes_same_repr(self) -> None:
        """Test typed=True separates types that share the same repr."""

        # Arrange: two classes with identical __repr__
        class A:
            def __repr__(self) -> str:
                return "X"

        class B:
            def __repr__(self) -> str:
                return "X"

        cache = MockCacheBackend()
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


class TestCacheBackendCacheControl:
    """Test cache_info() and cache_clear() when using CacheBackend backend."""

    async def test_cache_info_tracks_hits_and_misses(self) -> None:
        """Test that cache_info() returns accurate hit/miss statistics."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        # Act: first two calls miss, third hits
        await fetch(1)  # miss
        await fetch(2)  # miss
        await fetch(1)  # hit
        info = fetch.cache_info()  # type: ignore[attr-defined]

        # Assert
        assert info.hits == EXPECTED_HITS_1
        assert info.misses == EXPECTED_MISSES_2
        assert info.currsize == EXPECTED_CURRSIZE_2

    async def test_cache_clear_is_coroutine(self) -> None:
        """Test that cache_clear() on a CacheBackend-backed function is a coroutine."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        # Assert: cache_clear is async when backend is CacheBackend
        assert asyncio.iscoroutinefunction(fetch.cache_clear)  # type: ignore[attr-defined]

    async def test_cache_clear_empties_the_cache(self) -> None:
        """Test that awaiting cache_clear() causes subsequent calls to recompute."""
        # Arrange
        cache = MockCacheBackend()
        call_count = 0

        @cached(cache)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        await fetch(1)
        assert call_count == EXPECTED_CALL_COUNT_1

        # Act: clear the cache and call again
        await fetch.cache_clear()  # type: ignore[attr-defined]
        await fetch(1)

        # Assert: recomputed after clear
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_cache_info_currsize_resets_after_clear(self) -> None:
        """Test that currsize reported by cache_info drops to zero after clear."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        await fetch(1)
        await fetch(2)
        assert fetch.cache_info().currsize == EXPECTED_CURRSIZE_2  # type: ignore[attr-defined]

        # Act
        await fetch.cache_clear()  # type: ignore[attr-defined]

        # Assert
        assert fetch.cache_info().currsize == 0  # type: ignore[attr-defined]


class TestCacheBackendFunctionMetadata:
    """Test that @cached preserves decorated function metadata."""

    async def test_preserves_name(self) -> None:
        """Test that the async wrapper preserves __name__."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache)
        async def my_async_function() -> None:
            pass

        # Assert
        assert my_async_function.__name__ == "my_async_function"

    async def test_preserves_doc(self) -> None:
        """Test that the async wrapper preserves __doc__."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache)
        async def documented() -> None:
            """Return nothing."""

        # Assert
        assert documented.__doc__ == "Return nothing."


class TestCacheBackendLock:
    """Test @cached with CacheBackend and lock-based stampede protection."""

    async def test_lock_true_prevents_duplicate_computation(self) -> None:
        """Test that lock=True ensures only one coroutine computes on concurrent miss."""
        # Arrange
        cache = MockCacheBackend()
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
        """Test that lock=True uses per-key locks: different keys run in parallel."""
        # Arrange
        cache = MockCacheBackend()
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

        # Both tasks should have started because they hold independent per-key locks
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

    async def test_custom_asyncio_lock_prevents_duplicate_computation(
        self,
    ) -> None:
        """Test that a custom asyncio.Lock provides global stampede protection."""
        # Arrange
        cache = MockCacheBackend()
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

        # Assert: global lock still serializes concurrent same-key misses
        assert call_count == EXPECTED_CALL_COUNT_1

    async def test_lock_true_cache_hit_does_not_acquire_lock(self) -> None:
        """Test that a cache hit returns immediately without touching the lock."""
        # Arrange
        cache = MockCacheBackend()

        @cached(cache, lock=True)
        async def fetch(x: int) -> int:
            return x

        # Populate the cache
        await fetch(1)

        # Act: second call should be a cache hit, no lock needed
        result = await fetch(1)

        # Assert
        assert result == 1
        assert cache.cache_info().hits == EXPECTED_HITS_1


class TestCacheBackendSyncFunctionError:
    """Test that decorating a sync function with CacheBackend raises TypeError."""

    def test_sync_function_with_async_cache_raises_type_error(self) -> None:
        """Test that using CacheBackend with a sync function is rejected at decoration time."""
        # Arrange
        cache = MockCacheBackend()

        # Act / Assert
        with pytest.raises(
            TypeError,
            match="Sync functions cannot use a CacheBackend backend",
        ):

            @cached(cache)
            def compute(x: int) -> int:
                return x * 2

    def test_error_message_mentions_async(self) -> None:
        """Test that the TypeError message guides the user toward async solutions."""
        # Arrange
        cache = MockCacheBackend()

        # Act
        with pytest.raises(TypeError) as exc_info:

            @cached(cache)
            def sync_fn() -> None:
                pass

        # Assert: message contains actionable guidance
        message = str(exc_info.value)
        assert "async" in message.lower()
