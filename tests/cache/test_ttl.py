"""Test TTLCache."""

from __future__ import annotations

from time import monotonic
from unittest.mock import patch

import pytest

from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.cache.serializers import JsonSerializer, PickleSerializer
from grelmicro.cache.ttl import CacheInfo, TTLCache

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

EXPECTED_HITS_2 = 2
EXPECTED_OVERWRITE_VALUE = 10
EXPECTED_UNLIMITED_COUNT = 1000


@pytest.fixture
def backend() -> MemoryCacheBackend:
    """Provide an isolated in-memory cache backend (not registered globally)."""
    return MemoryCacheBackend()


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestInit:
    """Test TTLCache initialization and constructor validation."""

    def test_valid_params(self, backend: MemoryCacheBackend) -> None:
        """Test creating a cache with valid parameters."""
        # Act
        cache = TTLCache(maxsize=100, ttl=60, backend=backend)

        # Assert: initial currsize is 0 (no LRU entries yet)
        info = cache.cache_info()
        assert info.currsize == 0
        expected_maxsize = 100
        assert info.maxsize == expected_maxsize

    def test_unlimited_maxsize(self, backend: MemoryCacheBackend) -> None:
        """Test creating a cache with unlimited maxsize (maxsize=0)."""
        # Act
        cache = TTLCache(maxsize=0, ttl=60, backend=backend)

        # Assert
        info = cache.cache_info()
        assert info.maxsize == 0
        assert info.currsize == 0

    def test_negative_maxsize_raises(self, backend: MemoryCacheBackend) -> None:
        """Test that negative maxsize raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="maxsize must be non-negative"):
            TTLCache(maxsize=-1, ttl=60, backend=backend)

    def test_zero_ttl_raises(self, backend: MemoryCacheBackend) -> None:
        """Test that zero ttl raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLCache(maxsize=10, ttl=0, backend=backend)

    def test_negative_ttl_raises(self, backend: MemoryCacheBackend) -> None:
        """Test that negative ttl raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLCache(maxsize=10, ttl=-1, backend=backend)

    def test_default_backend_resolved_lazily(self) -> None:
        """Test that TTLCache created without backend resolves it lazily."""
        # Act: should not raise at construction time even without a registered backend
        cache = TTLCache(maxsize=0, ttl=60)

        # Assert: cache_info works without touching the backend
        info = cache.cache_info()
        assert info.hits == 0

    async def test_default_backend_from_registry(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that TTLCache uses the registered backend when none is provided."""
        # Arrange: register the backend
        cache_backend_registry.register(backend, "default")

        # Act: create cache without explicit backend
        cache = TTLCache(maxsize=0, ttl=60)
        await cache.set("registry_test", b"works")
        result = await cache.get("registry_test")

        # Assert
        assert result == b"works"

        # Cleanup
        cache_backend_registry.reset()


# ---------------------------------------------------------------------------
# Get / Set round-trip
# ---------------------------------------------------------------------------


class TestGetSet:
    """Test TTLCache get and set operations."""

    async def test_set_and_get_bytes(self, backend: MemoryCacheBackend) -> None:
        """Test setting and getting raw bytes without a serializer."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        await cache.set("key", b"hello")
        result = await cache.get("key")

        # Assert
        assert result == b"hello"

    async def test_set_and_get_with_serializer(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test get/set round-trip using json serializer/deserializer."""
        # Arrange
        cache = TTLCache(
            maxsize=10,
            ttl=60,
            backend=backend,
            serializer=JsonSerializer(),
        )

        # Act
        await cache.set("key", {"x": 1})
        result = await cache.get("key")

        # Assert
        assert result == {"x": 1}

    async def test_get_missing_key_returns_none(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test getting a missing key returns None by default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        result = await cache.get("missing")

        # Assert
        assert result is None

    async def test_get_missing_key_with_default(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test getting a missing key returns the supplied default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        result = await cache.get("missing", "fallback")

        # Assert
        assert result == "fallback"

    async def test_set_overwrites_existing(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that setting an existing key overwrites the value."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("key", b"old")

        # Act
        await cache.set("key", b"new")
        result = await cache.get("key")

        # Assert: value updated, LRU list still has one entry
        assert result == b"new"
        assert cache.cache_info().currsize == 1

    async def test_set_non_bytes_without_serializer_raises(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that setting a non-bytes value without a serializer raises TypeError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(
            TypeError, match="Cannot store str without a serializer"
        ):
            await cache.set("key", "not-bytes")

    async def test_set_invalid_per_entry_ttl_zero_raises(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that per-entry ttl=0 raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            await cache.set("key", b"v", ttl=0)

    async def test_set_invalid_per_entry_ttl_negative_raises(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that negative per-entry ttl raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            await cache.set("key", b"v", ttl=-5)


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestExpiry:
    """Test TTLCache TTL expiry behavior."""

    async def test_entry_expires_at_ttl_boundary(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that an entry is gone once the TTL has elapsed."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5, backend=backend)
        now = monotonic()

        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            await cache.set("key", b"value")

        # Assert: still present just before expiry
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 4):
            assert await cache.get("key") == b"value"

        # Assert: gone at exactly the TTL boundary
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 5):
            assert await cache.get("key") is None

    async def test_per_entry_ttl_override(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that a per-entry TTL overrides the cache-level default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        now = monotonic()

        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            await cache.set("key", b"value", ttl=10)

        # Assert: still alive before override expiry
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 9):
            assert await cache.get("key") == b"value"

        # Assert: expired at override boundary (not the 60 s default)
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 10):
            assert await cache.get("key") is None

    async def test_expired_entry_counts_as_miss(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that a get on an expired entry increments misses, not hits."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5, backend=backend)
        now = monotonic()

        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            await cache.set("key", b"value")

        # Act: hit before expiry
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 4):
            await cache.get("key")

        # Act: miss after expiry
        with patch("grelmicro.cache.memory.monotonic", return_value=now + 5):
            await cache.get("key")

        # Assert
        info = cache.cache_info()
        assert info.hits == 1
        assert info.misses == 1


# ---------------------------------------------------------------------------
# LRU eviction and maxsize
# ---------------------------------------------------------------------------


class TestEviction:
    """Test TTLCache LRU eviction and maxsize enforcement."""

    async def test_lru_eviction_when_full(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that the least recently used entry is evicted when cache is full."""
        # Arrange
        cache = TTLCache(maxsize=2, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")

        # Act: adding a third entry evicts "a" (LRU)
        await cache.set("c", b"3")

        # Assert
        assert await cache.get("a") is None
        assert await cache.get("b") == b"2"
        assert await cache.get("c") == b"3"

    async def test_get_promotes_to_mru(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that get() promotes an entry to most-recently-used position."""
        # Arrange: a=LRU, b, c=MRU
        cache = TTLCache(maxsize=3, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")
        await cache.set("c", b"3")

        # Act: get "a" promotes it, making "b" the new LRU
        await cache.get("a")
        await cache.set("d", b"4")

        # Assert: "b" was evicted
        assert await cache.get("b") is None
        assert await cache.get("a") is not None
        assert await cache.get("c") is not None
        assert await cache.get("d") is not None

    async def test_overwrite_promotes_to_mru(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that overwriting an existing key promotes it to MRU."""
        # Arrange: a=LRU, b, c=MRU
        cache = TTLCache(maxsize=3, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")
        await cache.set("c", b"3")

        # Act: overwrite "a" promotes it, making "b" the new LRU
        await cache.set("a", str(EXPECTED_OVERWRITE_VALUE).encode())
        await cache.set("d", b"4")

        # Assert: "b" was evicted; "a" kept with new value
        assert await cache.get("b") is None
        assert await cache.get("a") == str(EXPECTED_OVERWRITE_VALUE).encode()
        assert await cache.get("c") is not None
        assert await cache.get("d") is not None

    async def test_unlimited_maxsize_no_eviction(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that maxsize=0 allows unlimited entries without eviction."""
        # Arrange
        cache = TTLCache(maxsize=0, ttl=60, backend=backend)

        # Act: insert many entries
        for i in range(EXPECTED_UNLIMITED_COUNT):
            await cache.set(str(i), str(i).encode())

        # Assert: all entries still present (no evictions, no LRU tracking)
        info = cache.cache_info()
        assert info.evictions == 0
        # currsize is 0 when maxsize=0 because LRU list is not maintained
        assert info.currsize == 0

    async def test_evictions_tracked_in_cache_info(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that eviction count increments correctly in cache_info."""
        # Arrange
        cache = TTLCache(maxsize=2, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")

        # Act: triggers one eviction
        await cache.set("c", b"3")

        # Assert
        assert cache.cache_info().evictions == 1


# ---------------------------------------------------------------------------
# Delete and clear
# ---------------------------------------------------------------------------


class TestDeleteClear:
    """Test TTLCache delete and clear operations."""

    async def test_delete_existing_key(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that deleting an existing key removes it from the cache."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("key", b"value")

        # Act
        await cache.delete("key")

        # Assert
        assert await cache.get("key") is None
        assert cache.cache_info().currsize == 0

    async def test_delete_missing_key_is_noop(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that deleting a non-existent key does not raise."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert: no exception
        await cache.delete("missing")

    async def test_delete_removes_from_lru_tracker(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that delete removes the key from the LRU tracking list."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")

        # Act
        await cache.delete("a")

        # Assert: LRU list reflects removal
        assert cache.cache_info().currsize == 1

    async def test_clear_removes_all_entries(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that clear() removes all entries and resets LRU tracking."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"1")
        await cache.set("b", b"2")

        # Act
        await cache.clear()

        # Assert
        assert await cache.get("a") is None
        assert await cache.get("b") is None
        assert cache.cache_info().currsize == 0


# ---------------------------------------------------------------------------
# cache_info statistics
# ---------------------------------------------------------------------------


class TestCacheInfo:
    """Test TTLCache cache_info statistics and CacheInfo immutability."""

    def test_initial_stats(self, backend: MemoryCacheBackend) -> None:
        """Test that a freshly created cache has zero stats."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        info = cache.cache_info()

        # Assert
        assert info == CacheInfo(
            hits=0, misses=0, maxsize=10, currsize=0, evictions=0
        )

    async def test_hits_and_misses_tracked(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that hits and misses accumulate correctly."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("key", b"value")

        # Act: two hits, one miss
        await cache.get("key")
        await cache.get("key")
        await cache.get("missing")

        # Assert
        info = cache.cache_info()
        assert info.hits == EXPECTED_HITS_2
        assert info.misses == 1
        assert info.currsize == 1

    def test_cache_info_is_immutable(self, backend: MemoryCacheBackend) -> None:
        """Test that CacheInfo instances are frozen dataclasses."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        info = cache.cache_info()

        # Act / Assert
        with pytest.raises(AttributeError):
            info.hits = 99  # type: ignore[misc]  # ty: ignore[invalid-assignment]

    async def test_currsize_reflects_lru_tracker(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that currsize in cache_info reflects the LRU key list length."""
        # Arrange
        cache = TTLCache(maxsize=5, ttl=60, backend=backend)

        # Act
        await cache.set("x", b"1")
        await cache.set("y", b"2")

        # Assert
        expected_size = 2
        assert cache.cache_info().currsize == expected_size

    async def test_currsize_zero_for_unlimited(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that currsize is 0 for unlimited caches (no LRU tracking)."""
        # Arrange
        cache = TTLCache(maxsize=0, ttl=60, backend=backend)

        # Act
        await cache.set("x", b"1")

        # Assert: LRU list not maintained when maxsize=0
        assert cache.cache_info().currsize == 0


# ---------------------------------------------------------------------------
# Serializer / deserializer
# ---------------------------------------------------------------------------


class TestSerializer:
    """Test TTLCache serializer and deserializer integration."""

    async def test_json_round_trip(self, backend: MemoryCacheBackend) -> None:
        """Test a full JSON serializer/deserializer round-trip."""
        # Arrange
        cache = TTLCache(
            maxsize=10,
            ttl=60,
            backend=backend,
            serializer=JsonSerializer(),
        )

        # Act
        await cache.set("user", {"id": 42, "name": "alice"})
        result = await cache.get("user")

        # Assert
        assert result == {"id": 42, "name": "alice"}

    async def test_pickle_serializer(self, backend: MemoryCacheBackend) -> None:
        """Test serializing with PickleSerializer."""
        # Arrange
        cache = TTLCache(
            maxsize=10,
            ttl=60,
            backend=backend,
            serializer=PickleSerializer(),
        )

        # Act
        await cache.set("count", 99)
        result = await cache.get("count")

        # Assert
        expected = 99
        assert result == expected

    async def test_no_serializer_bytes_pass_through(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that raw bytes are stored and returned unchanged without a serializer."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        payload = b"\x00\xff\xfe binary data"

        # Act
        await cache.set("raw", payload)
        result = await cache.get("raw")

        # Assert
        assert result == payload

    async def test_type_error_for_non_bytes_without_serializer(
        self, backend: MemoryCacheBackend
    ) -> None:
        """Test that storing a list without a serializer raises TypeError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(
            TypeError, match="Cannot store list without a serializer"
        ):
            await cache.set("key", [1, 2, 3])
