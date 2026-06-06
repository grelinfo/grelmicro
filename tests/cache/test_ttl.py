"""Test TTLCache."""

from __future__ import annotations

import asyncio
from time import monotonic
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from grelmicro import Grelmicro
from grelmicro.cache import Cache, TTLCacheConfig
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer, PickleSerializer
from grelmicro.cache.ttl import CacheInfo, TTLCache

pytestmark = [pytest.mark.timeout(10)]

EXPECTED_HITS_2 = 2
EXPECTED_OVERWRITE_VALUE = 10
EXPECTED_UNLIMITED_COUNT = 1000


@pytest.fixture
def backend() -> MemoryCacheAdapter:
    """Provide an isolated in-memory cache backend (not registered globally)."""
    return MemoryCacheAdapter()


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestInit:
    """Test TTLCache initialization and constructor validation."""

    def test_valid_params(self, backend: MemoryCacheAdapter) -> None:
        """Test creating a cache with valid parameters."""
        # Act
        cache = TTLCache(maxsize=100, ttl=60, backend=backend)

        # Assert: initial currsize is 0 (no LRU entries yet)
        info = cache.cache_info()
        assert info.currsize == 0
        expected_maxsize = 100
        assert info.maxsize == expected_maxsize

    def test_unlimited_maxsize(self, backend: MemoryCacheAdapter) -> None:
        """Test creating a cache with unlimited maxsize (maxsize=0)."""
        # Act
        cache = TTLCache(maxsize=0, ttl=60, backend=backend)

        # Assert
        info = cache.cache_info()
        assert info.maxsize == 0
        assert info.currsize == 0

    def test_negative_maxsize_raises(self, backend: MemoryCacheAdapter) -> None:
        """Test that negative maxsize raises a Pydantic validation error."""
        # Act / Assert
        with pytest.raises(ValidationError, match="maxsize"):
            TTLCache(maxsize=-1, ttl=60, backend=backend)

    def test_zero_ttl_raises(self, backend: MemoryCacheAdapter) -> None:
        """Test that zero ttl raises a Pydantic validation error."""
        # Act / Assert
        with pytest.raises(ValidationError, match="ttl"):
            TTLCache(maxsize=10, ttl=0, backend=backend)

    def test_negative_ttl_raises(self, backend: MemoryCacheAdapter) -> None:
        """Test that negative ttl raises a Pydantic validation error."""
        # Act / Assert
        with pytest.raises(ValidationError, match="ttl"):
            TTLCache(maxsize=10, ttl=-1, backend=backend)

    def test_config_property(self, backend: MemoryCacheAdapter) -> None:
        """Test that `config` exposes the frozen `TTLCacheConfig`."""
        # Arrange / Act
        cache = TTLCache(maxsize=50, ttl=30, backend=backend)

        # Assert
        assert isinstance(cache.config, TTLCacheConfig)
        expected_maxsize = 50
        expected_ttl = 30
        assert cache.config.maxsize == expected_maxsize
        assert cache.config.ttl == expected_ttl

    def test_default_backend_resolved_lazily(self) -> None:
        """Test that TTLCache created without backend resolves it lazily."""
        # Act: should not raise at construction time even without a registered backend
        cache = TTLCache(maxsize=0, ttl=60)

        # Assert: cache_info works without touching the backend
        info = cache.cache_info()
        assert info.hits == 0

    async def test_default_backend_from_active_app(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that TTLCache resolves through the active `Grelmicro` app."""
        async with Grelmicro(uses=[Cache(backend)]):
            cache = TTLCache(maxsize=0, ttl=60)
            await cache.set("app_test", b"works")
            result = await cache.get("app_test")

        assert result == b"works"


# ---------------------------------------------------------------------------
# Get / Set round-trip
# ---------------------------------------------------------------------------


class TestGetSet:
    """Test TTLCache get and set operations."""

    async def test_set_and_get_bytes(self, backend: MemoryCacheAdapter) -> None:
        """Test setting and getting raw bytes without a serializer."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        await cache.set("key", b"hello")
        result = await cache.get("key")

        # Assert
        assert result == b"hello"

    async def test_set_and_get_with_serializer(
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test getting a missing key returns None by default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        result = await cache.get("missing")

        # Assert
        assert result is None

    async def test_get_missing_key_with_default(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test getting a missing key returns the supplied default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act
        result = await cache.get("missing", "fallback")

        # Assert
        assert result == "fallback"

    async def test_set_overwrites_existing(
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that per-entry ttl=0 raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            await cache.set("key", b"v", ttl=0)

    async def test_set_invalid_per_entry_ttl_negative_raises(
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that deleting a non-existent key does not raise."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert: no exception
        await cache.delete("missing")

    async def test_delete_removes_from_lru_tracker(
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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

    def test_initial_stats(self, backend: MemoryCacheAdapter) -> None:
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
        self, backend: MemoryCacheAdapter
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

    def test_cache_info_is_immutable(self, backend: MemoryCacheAdapter) -> None:
        """Test that CacheInfo instances are frozen dataclasses."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        info = cache.cache_info()

        # Act / Assert
        with pytest.raises(AttributeError):
            info.hits = 99  # type: ignore[misc]  # ty: ignore[invalid-assignment]

    async def test_currsize_reflects_lru_tracker(
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
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

    async def test_json_round_trip(self, backend: MemoryCacheAdapter) -> None:
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

    async def test_pickle_serializer(self, backend: MemoryCacheAdapter) -> None:
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
        self, backend: MemoryCacheAdapter
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
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that storing a list without a serializer raises TypeError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)

        # Act / Assert
        with pytest.raises(
            TypeError, match="Cannot store list without a serializer"
        ):
            await cache.set("key", [1, 2, 3])


class TestGetOrSet:
    """Tests for TTLCache.get_or_set."""

    async def test_cold_path_computes_and_stores(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that a miss runs the factory, stores, and returns the value."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        calls = 0

        def factory() -> dict:
            nonlocal calls
            calls += 1
            return {"v": 1}

        result = await cache.get_or_set("k", factory)

        assert result == {"v": 1}
        assert calls == 1
        assert await cache.get("k") == {"v": 1}

    async def test_fast_path_returns_cached_without_calling_factory(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that a hit returns the cached value and skips the factory."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        await cache.set("k", {"v": 1})
        calls = 0

        def factory() -> dict:
            nonlocal calls
            calls += 1
            return {"v": 2}

        result = await cache.get_or_set("k", factory)

        assert result == {"v": 1}
        assert calls == 0

    async def test_async_factory_is_awaited(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that an async factory is detected and awaited."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        async def factory() -> dict:
            return {"v": 42}

        result = await cache.get_or_set("k", factory)

        assert result == {"v": 42}

    async def test_records_hit_on_fast_path(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that a hit increments the hit counter once."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        await cache.set("k", {"v": 1})

        await cache.get_or_set("k", lambda: {"v": 2})

        assert cache.cache_info().hits == 1
        assert cache.cache_info().misses == 0

    async def test_records_single_miss_on_cold_path(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that a cold miss is counted exactly once."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        await cache.get_or_set("k", lambda: {"v": 1})

        assert cache.cache_info().misses == 1
        assert cache.cache_info().hits == 0

    async def test_stores_tags(self, backend: MemoryCacheAdapter) -> None:
        """Test that get_or_set associates tags with the computed entry."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        await cache.get_or_set("k", lambda: {"v": 1}, tags=["t"])

        assert backend._tag_keys["t"] == {"cache:k"}

    async def test_stampede_folds_concurrent_misses(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that concurrent misses compute the factory once."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        calls = 0
        barrier = asyncio.Event()

        async def factory() -> dict:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return {"v": calls}

        task_a = asyncio.create_task(cache.get_or_set("k", factory))
        task_b = asyncio.create_task(cache.get_or_set("k", factory))
        await asyncio.sleep(0.05)
        barrier.set()

        result_a = await task_a
        result_b = await task_b

        assert calls == 1
        assert result_a == result_b == {"v": 1}


class TestGetMany:
    """Tests for TTLCache.get_many."""

    async def test_returns_found_only(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that get_many returns only the keys present."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        await cache.set("a", {"v": 1})
        await cache.set("b", {"v": 2})

        result = await cache.get_many(["a", "b", "missing"])

        assert result == {"a": {"v": 1}, "b": {"v": 2}}

    async def test_empty_keys_returns_empty(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that an empty key list returns an empty dict."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        assert await cache.get_many([]) == {}

    async def test_records_hit_per_found_and_miss_per_absent(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that stats count one hit per found and one miss per absent."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        await cache.set("a", {"v": 1})

        await cache.get_many(["a", "b", "c"])

        info = cache.cache_info()
        assert info.hits == 1
        assert info.misses == 2  # noqa: PLR2004

    async def test_promotes_found_keys_in_lru(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that get_many promotes found keys when maxsize is set."""
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"a")
        await cache.set("b", b"b")

        result = await cache.get_many(["a"])

        assert result == {"a": b"a"}
        assert "a" in cache._keys


class TestSetMany:
    """Tests for TTLCache.set_many."""

    async def test_stores_all(self, backend: MemoryCacheAdapter) -> None:
        """Test that set_many writes every pair."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        await cache.set_many({"a": {"v": 1}, "b": {"v": 2}})

        assert await cache.get("a") == {"v": 1}
        assert await cache.get("b") == {"v": 2}

    async def test_empty_mapping_is_no_op(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that an empty mapping does nothing."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        await cache.set_many({})

    async def test_rejects_non_positive_ttl(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that a non-positive ttl raises."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        with pytest.raises(ValueError, match="ttl must be positive"):
            await cache.set_many({"a": {"v": 1}}, ttl=0)

    async def test_stores_tags(self, backend: MemoryCacheAdapter) -> None:
        """Test that set_many associates tags with every key."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())

        await cache.set_many({"a": {"v": 1}, "b": {"v": 2}}, tags=["g"])

        assert backend._tag_keys["g"] == {"cache:a", "cache:b"}

    async def test_evicts_when_over_maxsize(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that set_many enforces maxsize via LRU eviction."""
        cache = TTLCache(maxsize=2, ttl=60, backend=backend)
        await cache.set("old", b"old")

        await cache.set_many({"a": b"a", "b": b"b"})

        assert cache.cache_info().currsize == 2  # noqa: PLR2004
        assert cache.cache_info().evictions >= 1

    async def test_promotes_existing_key(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that set_many promotes an already-tracked key without evicting."""
        cache = TTLCache(maxsize=2, ttl=60, backend=backend)
        await cache.set("a", b"a")
        await cache.set("b", b"b")

        await cache.set_many({"a": b"a2"})

        assert await cache.get("a") == b"a2"
        assert cache.cache_info().currsize == 2  # noqa: PLR2004


class TestDeleteMany:
    """Tests for TTLCache.delete_many."""

    async def test_deletes_all(self, backend: MemoryCacheAdapter) -> None:
        """Test that delete_many removes every listed key."""
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"a")
        await cache.set("b", b"b")

        await cache.delete_many(["a", "b"])

        assert await cache.get("a") is None
        assert await cache.get("b") is None
        assert "a" not in cache._keys
        assert "b" not in cache._keys

    async def test_empty_keys_is_no_op(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that an empty key list does nothing."""
        cache = TTLCache(ttl=60, backend=backend)

        await cache.delete_many([])


class TestDeleteTags:
    """Tests for TTLCache.delete_tags."""

    async def test_deletes_tagged_entries(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that delete_tags removes every entry sharing a tag."""
        cache = TTLCache(ttl=60, backend=backend, serializer=JsonSerializer())
        await cache.set("a", {"v": 1}, tags=["g"])
        await cache.set("b", {"v": 2}, tags=["g"])
        await cache.set("c", {"v": 3}, tags=["other"])

        await cache.delete_tags("g")

        assert await cache.get("a") is None
        assert await cache.get("b") is None
        assert await cache.get("c") == {"v": 3}

    async def test_no_tags_is_no_op(self, backend: MemoryCacheAdapter) -> None:
        """Test that calling delete_tags with no tags does nothing."""
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"a")

        await cache.delete_tags()

        assert await cache.get("a") == b"a"
        assert "a" in cache._keys

    async def test_clears_local_lru(self, backend: MemoryCacheAdapter) -> None:
        """Test that delete_tags clears the local LRU bookkeeping."""
        cache = TTLCache(maxsize=10, ttl=60, backend=backend)
        await cache.set("a", b"a", tags=["g"])

        await cache.delete_tags("g")

        assert len(cache._keys) == 0


class TestSetWithTags:
    """Tests for tags on TTLCache.set."""

    async def test_set_associates_tags(
        self, backend: MemoryCacheAdapter
    ) -> None:
        """Test that set forwards tags to the backend."""
        cache = TTLCache(ttl=60, backend=backend)

        await cache.set("a", b"a", tags=["t1", "t2"])

        assert backend._tag_keys["t1"] == {"cache:a"}
        assert backend._tag_keys["t2"] == {"cache:a"}
