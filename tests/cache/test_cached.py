"""Test Cached Decorator."""

import json

import pytest

from grelmicro.cache.cached import cached
from grelmicro.cache.ttl import TTLCache

pytestmark = [pytest.mark.anyio]

EXPECTED_DOUBLE_5 = 10
EXPECTED_CALL_COUNT_2 = 2


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
