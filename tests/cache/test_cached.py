"""Test Cached Decorator."""

import asyncio
import json
import threading
import time

import pytest

from grelmicro.cache.cached import cached
from grelmicro.cache.ttl import TTLCache

pytestmark = [pytest.mark.anyio]

EXPECTED_DOUBLE_5 = 10
EXPECTED_CALL_COUNT_2 = 2
EXPECTED_MISSES_2 = 2
EXPECTED_CURRSIZE_2 = 2


class TestSyncCached:
    """Test cached decorator with sync functions."""

    def test_caches_result(self) -> None:
        """Test that repeated calls return cached result."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        first = compute(5)
        second = compute(5)

        # Assert
        assert first == EXPECTED_DOUBLE_5
        assert second == EXPECTED_DOUBLE_5
        assert call_count == 1

    def test_different_args_different_keys(self) -> None:
        """Test that different arguments produce different keys."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        compute(1)
        compute(2)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    def test_kwargs_are_part_of_key(self) -> None:
        """Test that kwargs are included in the cache key."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache)
        def greet(name: str, *, greeting: str = "hi") -> str:
            nonlocal call_count
            call_count += 1
            return f"{greeting} {name}"

        # Act
        greet("alice", greeting="hello")
        greet("alice", greeting="hey")

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2


class TestAsyncCached:
    """Test cached decorator with async functions."""

    async def test_caches_result(self) -> None:
        """Test that repeated async calls return cached result."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
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
        assert call_count == 1

    async def test_different_args_different_keys(self) -> None:
        """Test that different args produce different cache entries."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
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


class TestCustomKeyMaker:
    """Test cached decorator with custom key_maker."""

    def test_custom_key_maker(self) -> None:
        """Test that custom key_maker is used for cache keys."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(
            cache,
            key_maker=lambda _func, args, _kwargs: str(args[0]),
        )
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        compute(5)
        compute(5)

        # Assert
        assert call_count == 1

    async def test_custom_key_maker_async(self) -> None:
        """Test that custom key_maker works with async functions."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(
            cache,
            key_maker=lambda _func, args, _kwargs: str(args[0]),
        )
        async def fetch(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        # Act
        await fetch(1)
        await fetch(1)

        # Assert
        assert call_count == 1


class TestSerializer:
    """Test cached decorator with serializer/deserializer."""

    def test_with_serializer(self) -> None:
        """Test that serializer and deserializer are applied."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(
            cache,
            serializer=lambda v: json.dumps(v).encode(),
            deserializer=json.loads,
        )
        def get_data() -> dict:
            return {"key": "value"}

        # Act
        first = get_data()
        second = get_data()

        # Assert
        assert first == {"key": "value"}
        assert second == {"key": "value"}

    async def test_with_serializer_async(self) -> None:
        """Test serializer/deserializer with async functions."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

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

        # Assert
        assert first == {"key": "value"}
        assert second == {"key": "value"}


class TestSerializerValidation:
    """Test that serializer and deserializer must be paired."""

    def test_serializer_without_deserializer(self) -> None:
        """Test that providing only serializer raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act / Assert
        with pytest.raises(
            ValueError,
            match="serializer and deserializer must be provided together",
        ):
            cached(cache, serializer=lambda v: json.dumps(v).encode())

    def test_deserializer_without_serializer(self) -> None:
        """Test that providing only deserializer raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act / Assert
        with pytest.raises(
            ValueError,
            match="serializer and deserializer must be provided together",
        ):
            cached(cache, deserializer=json.loads)


class TestSkip:
    """Test cached decorator with skip condition."""

    def test_skip_none_results(self) -> None:
        """Test that None results are not cached when skip is set."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        maybe_fetch(found=False)  # returns None, not cached
        maybe_fetch(found=False)  # still calls function

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    def test_skip_does_not_affect_valid_results(self) -> None:
        """Test that valid results are still cached with skip."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        maybe_fetch(found=True)  # cached
        maybe_fetch(found=True)  # cache hit

        # Assert
        assert call_count == 1

    async def test_skip_async(self) -> None:
        """Test skip condition with async functions."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, skip=lambda r: r is None)
        async def maybe_fetch(*, found: bool) -> str | None:
            nonlocal call_count
            call_count += 1
            return "data" if found else None

        # Act
        await maybe_fetch(found=False)
        await maybe_fetch(found=False)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2


class TestTyped:
    """Test cached decorator with typed key generation."""

    def test_typed_distinguishes_int_and_float(self) -> None:
        """Test that typed=True caches 3 and 3.0 separately."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, typed=True)
        def compute(x: float) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act
        compute(3)
        compute(3.0)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2

    def test_typed_distinguishes_same_repr(self) -> None:
        """Test typed=True distinguishes args with same repr."""

        # Arrange — two types with identical repr
        class A:
            def __repr__(self) -> str:
                return "X"

        class B:
            def __repr__(self) -> str:
                return "X"

        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, typed=True)
        def identity(x: object) -> str:
            nonlocal call_count
            call_count += 1
            return type(x).__name__

        # Act — same repr, different types
        identity(A())
        identity(B())

        # Assert — typed ensures separate cache entries
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_typed_async(self) -> None:
        """Test typed key generation with async functions."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
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


class TestCacheControlMethods:
    """Test cache_info() and cache_clear() on decorated functions."""

    def test_cache_info(self) -> None:
        """Test that cache_info() returns statistics."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(cache)
        def compute(x: int) -> int:
            return x * 2

        # Act
        compute(1)  # miss
        compute(1)  # hit
        compute(2)  # miss
        info = compute.cache_info()  # type: ignore[attr-defined]

        # Assert
        assert info.hits == 1
        assert info.misses == EXPECTED_MISSES_2
        assert info.currsize == EXPECTED_CURRSIZE_2

    def test_cache_clear(self) -> None:
        """Test that cache_clear() empties the cache."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache)
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        compute(1)
        assert call_count == 1

        # Act
        compute.cache_clear()  # type: ignore[attr-defined]
        compute(1)

        # Assert — recomputed after clear
        assert call_count == EXPECTED_CALL_COUNT_2

    async def test_cache_info_async(self) -> None:
        """Test cache_info() on async decorated function."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(cache)
        async def fetch(x: int) -> int:
            return x

        # Act
        await fetch(1)  # miss
        await fetch(1)  # hit
        info = fetch.cache_info()  # type: ignore[attr-defined]

        # Assert
        assert info.hits == 1
        assert info.misses == 1

    async def test_cache_clear_async(self) -> None:
        """Test cache_clear() on async decorated function."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache)
        async def fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        await fetch(1)
        assert call_count == 1

        # Act
        fetch.cache_clear()  # type: ignore[attr-defined]
        await fetch(1)

        # Assert
        assert call_count == EXPECTED_CALL_COUNT_2


class TestFunctionMetadata:
    """Test that cached preserves function metadata."""

    def test_preserves_name(self) -> None:
        """Test that the wrapper preserves __name__."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(cache)
        def my_function() -> None:
            pass

        # Assert
        assert my_function.__name__ == "my_function"

    async def test_preserves_name_async(self) -> None:
        """Test that the async wrapper preserves __name__."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(cache)
        async def my_async_function() -> None:
            pass

        # Assert
        assert my_async_function.__name__ == "my_async_function"


class TestLock:
    """Test cached decorator with lock for stampede protection."""

    def test_lock_prevents_duplicate_computation(self) -> None:
        """Test that lock prevents redundant computation."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0

        @cached(cache, lock=threading.Lock())
        def compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Act
        compute(5)
        compute(5)

        # Assert
        assert call_count == 1

    async def test_async_lock_prevents_duplicate_computation(
        self,
    ) -> None:
        """Test that async lock prevents redundant computation."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0
        barrier = asyncio.Event()

        @cached(cache, lock=asyncio.Lock())
        async def slow_fetch(x: int) -> int:
            nonlocal call_count
            call_count += 1
            await barrier.wait()
            return x * 2

        # Act — launch two concurrent calls
        task1 = asyncio.create_task(slow_fetch(5))
        task2 = asyncio.create_task(slow_fetch(5))
        await asyncio.sleep(0)  # let tasks start
        barrier.set()
        await task1
        await task2

        # Assert — only one actual computation
        assert call_count == 1

    def test_sync_lock_concurrent_stampede(self) -> None:
        """Test that sync lock prevents duplicate computation under thread contention."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        call_count = 0
        started = threading.Event()

        @cached(cache, lock=threading.Lock())
        def slow_compute(x: int) -> int:
            nonlocal call_count
            call_count += 1
            started.set()
            time.sleep(0.1)
            return x * 2

        # Act — launch two threads hitting the same key concurrently
        results: list[int] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(slow_compute(5))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        started.wait(timeout=2)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Assert — only one computation, both get the same result
        assert not errors
        assert call_count == 1
        assert results == [10, 10]

    async def test_async_lock_cache_hit_skips_lock(self) -> None:
        """Test that cache hit doesn't acquire the lock."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        @cached(cache, lock=asyncio.Lock())
        async def fetch(x: int) -> int:
            return x

        # Act — first call populates cache
        await fetch(1)
        # Second call should hit cache without lock
        result = await fetch(1)

        # Assert
        assert result == 1
        assert cache.cache_info().hits == 1
