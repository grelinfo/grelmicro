"""Test TTLCache."""

from time import monotonic
from unittest.mock import patch

import pytest

from grelmicro.cache.ttl import CacheInfo, TTLCache

EXPECTED_EVICTION_LEN = 2
EXPECTED_HITS_2 = 2
EXPECTED_OVERWRITE_VALUE = 10
EXPECTED_UNLIMITED_LEN = 1000


class TestInit:
    """Test TTLCache initialization."""

    def test_valid_params(self) -> None:
        """Test creating a cache with valid parameters."""
        # Act
        cache = TTLCache(maxsize=100, ttl=60)

        # Assert
        assert len(cache) == 0

    def test_unlimited_maxsize(self) -> None:
        """Test creating a cache with unlimited maxsize."""
        # Act
        cache = TTLCache(maxsize=0, ttl=60)

        # Assert
        assert len(cache) == 0

    def test_negative_maxsize(self) -> None:
        """Test that negative maxsize raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="maxsize must be non-negative"):
            TTLCache(maxsize=-1, ttl=60)

    def test_zero_ttl(self) -> None:
        """Test that zero ttl raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLCache(maxsize=10, ttl=0)

    def test_negative_ttl(self) -> None:
        """Test that negative ttl raises ValueError."""
        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLCache(maxsize=10, ttl=-1)


class TestGetSet:
    """Test TTLCache get and set operations."""

    def test_set_and_get(self) -> None:
        """Test setting and getting a value."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act
        cache.set("key", "value")
        result = cache.get("key")

        # Assert
        assert result == "value"

    def test_get_missing_key(self) -> None:
        """Test getting a missing key returns default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act
        result = cache.get("missing")

        # Assert
        assert result is None

    def test_get_missing_key_with_default(self) -> None:
        """Test getting a missing key returns custom default."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act
        result = cache.get("missing", "fallback")

        # Assert
        assert result == "fallback"

    def test_set_overwrites_existing(self) -> None:
        """Test that setting an existing key overwrites the value."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        cache.set("key", "old")

        # Act
        cache.set("key", "new")

        # Assert
        assert cache.get("key") == "new"
        assert len(cache) == 1

    def test_set_with_custom_ttl(self) -> None:
        """Test setting a value with per-entry TTL override."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        now = monotonic()

        # Act
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value", ttl=10)

        # Assert — not expired at now + 9
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 9,
        ):
            assert cache.get("key") == "value"

        # Assert — expired at now + 10
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 10,
        ):
            assert cache.get("key") is None

    def test_set_invalid_ttl(self) -> None:
        """Test that zero or negative per-entry ttl raises ValueError."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act / Assert
        with pytest.raises(ValueError, match="ttl must be positive"):
            cache.set("key", "value", ttl=0)

        with pytest.raises(ValueError, match="ttl must be positive"):
            cache.set("key", "value", ttl=-1)


class TestExpiry:
    """Test TTLCache entry expiration."""

    def test_get_expired_entry(self) -> None:
        """Test that expired entries return default on get."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value")

        # Act — access after TTL
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            result = cache.get("key")

        # Assert
        assert result is None

    def test_contains_expired_entry(self) -> None:
        """Test that expired entries are not found by __contains__."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value")

        # Act / Assert — before expiry
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 4,
        ):
            assert "key" in cache

        # Act / Assert — at expiry
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            assert "key" not in cache


class TestExpiryCleansUp:
    """Test that expired entries are removed from internal data."""

    def test_get_removes_expired_entry(self) -> None:
        """Test that get() lazily removes expired entries."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value")

        # Act — access after TTL
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            cache.get("key")

        # Assert — entry was purged from internal storage
        assert len(cache) == 0

    def test_contains_does_not_remove_expired_entry(self) -> None:
        """Test that __contains__ does not purge expired entries (lazy cleanup via get/set only)."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value")

        # Act — check after TTL
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            result = "key" in cache

        # Assert — entry is expired but not purged (consistent with __len__)
        assert result is False
        assert len(cache) == 1


class TestEviction:
    """Test TTLCache eviction behavior."""

    def test_evict_oldest_expired(self) -> None:
        """Test that oldest expired entry is evicted first."""
        # Arrange
        cache = TTLCache(maxsize=2, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("a", 1)
            cache.set("b", 2)

        # Act — insert new entry after "a" has expired
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            cache.set("c", 3)

        # Assert — "a" was evicted (expired), "b" and "c" kept
        assert len(cache) == EXPECTED_EVICTION_LEN
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            assert cache.get("a") is None
            assert cache.get("c") is not None

    def test_evict_lru_when_none_expired(self) -> None:
        """Test LRU eviction when no entries are expired."""
        # Arrange
        cache = TTLCache(maxsize=2, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)

        # Act
        cache.set("c", 3)

        # Assert — "a" evicted (least recently used)
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None

    def test_contains_does_not_promote_to_mru(self) -> None:
        """Test that __contains__ does not affect LRU order."""
        # Arrange
        cache = TTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        # Act — check "a" via __contains__ (should NOT promote)
        assert "a" in cache
        cache.set("d", 4)

        # Assert — "a" still evicted (LRU), not "b"
        assert cache.get("a") is None
        assert cache.get("b") is not None

    def test_get_promotes_to_mru(self) -> None:
        """Test that get() promotes entry to most-recently-used."""
        # Arrange
        cache = TTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        # Act — access "a" to promote it, making "b" the LRU
        cache.get("a")
        cache.set("d", 4)

        # Assert — "b" evicted (LRU), "a" kept
        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert cache.get("c") is not None
        assert cache.get("d") is not None

    def test_overwrite_promotes_to_mru(self) -> None:
        """Test that overwriting a key promotes it to MRU."""
        # Arrange
        cache = TTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        # Act — overwrite "a", making "b" the LRU
        cache.set("a", EXPECTED_OVERWRITE_VALUE)
        cache.set("d", 4)

        # Assert — "b" evicted (LRU), "a" kept
        assert cache.get("b") is None
        assert cache.get("a") == EXPECTED_OVERWRITE_VALUE
        assert cache.get("c") is not None
        assert cache.get("d") is not None

    def test_unlimited_maxsize_no_eviction(self) -> None:
        """Test that unlimited maxsize does not evict."""
        # Arrange
        cache = TTLCache(maxsize=0, ttl=60)

        # Act
        for i in range(EXPECTED_UNLIMITED_LEN):
            cache.set(str(i), i)

        # Assert
        assert len(cache) == EXPECTED_UNLIMITED_LEN


class TestDeleteClear:
    """Test TTLCache delete and clear operations."""

    def test_delete_existing(self) -> None:
        """Test deleting an existing key."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        cache.set("key", "value")

        # Act
        cache.delete("key")

        # Assert
        assert cache.get("key") is None
        assert len(cache) == 0

    def test_delete_missing(self) -> None:
        """Test deleting a missing key is a no-op."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act / Assert — no exception raised
        cache.delete("missing")

    def test_clear(self) -> None:
        """Test clearing all entries."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)

        # Act
        cache.clear()

        # Assert
        assert len(cache) == 0


class TestContains:
    """Test TTLCache __contains__."""

    def test_contains_existing(self) -> None:
        """Test that existing key is found."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        cache.set("key", "value")

        # Act / Assert
        assert "key" in cache

    def test_contains_missing(self) -> None:
        """Test that missing key is not found."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act / Assert
        assert "key" not in cache


class TestCacheInfo:
    """Test TTLCache cache_info statistics."""

    def test_initial_stats(self) -> None:
        """Test that fresh cache has zero stats."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)

        # Act
        info = cache.cache_info()

        # Assert
        assert info == CacheInfo(
            hits=0, misses=0, maxsize=10, currsize=0, evictions=0
        )

    def test_hits_and_misses(self) -> None:
        """Test that hits and misses are tracked correctly."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        cache.set("key", "value")

        # Act
        cache.get("key")  # hit
        cache.get("key")  # hit
        cache.get("missing")  # miss

        # Assert
        info = cache.cache_info()
        assert info.hits == EXPECTED_HITS_2
        assert info.misses == 1
        assert info.currsize == 1

    def test_expired_entry_counts_as_miss(self) -> None:
        """Test that accessing an expired entry counts as a miss."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=5)
        now = monotonic()

        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now,
        ):
            cache.set("key", "value")

        # Act — hit before expiry
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 4,
        ):
            cache.get("key")

        # Act — miss after expiry
        with patch(
            "grelmicro.cache.ttl.monotonic",
            return_value=now + 5,
        ):
            cache.get("key")

        # Assert
        info = cache.cache_info()
        assert info.hits == 1
        assert info.misses == 1

    def test_evictions_tracked(self) -> None:
        """Test that evictions are counted."""
        # Arrange
        cache = TTLCache(maxsize=2, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)

        # Act — triggers eviction
        cache.set("c", 3)

        # Assert
        assert cache.cache_info().evictions == 1

    def test_cache_info_is_frozen(self) -> None:
        """Test that CacheInfo is immutable."""
        # Arrange
        cache = TTLCache(maxsize=10, ttl=60)
        info = cache.cache_info()

        # Act / Assert
        with pytest.raises(AttributeError):
            info.hits = 99  # type: ignore[misc]
