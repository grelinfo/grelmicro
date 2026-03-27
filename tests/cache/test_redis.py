"""Tests for Redis Cache Backend."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from grelmicro._backends import BackendNotLoadedError
from grelmicro.cache._backends import cache_backend_registry, get_cache_backend
from grelmicro.cache.errors import CacheSettingsValidationError
from grelmicro.cache.redis import RedisCache

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


class TestRedisCacheEnvVarSettings:
    """Tests for Redis Cache settings from environment variables."""

    @pytest.mark.parametrize(
        ("environs", "expected_url"),
        [
            (
                {"REDIS_URL": URL},
                URL,
            ),
            (
                {
                    "REDIS_HOST": "test_host",
                    "REDIS_PORT": "1234",
                    "REDIS_DB": "0",
                    "REDIS_PASSWORD": "test_password",
                },
                URL,
            ),
            (
                {"REDIS_HOST": "test_host"},
                "redis://test_host:6379/0",
            ),
        ],
    )
    def test_redis_env_var_settings(
        self,
        environs: dict[str, str],
        expected_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test Redis Cache URL resolved from environment variables."""
        # Arrange
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        # Act
        cache = RedisCache(ttl=60)

        # Assert
        assert cache._url == expected_url


class TestRedisCacheEnvVarValidationError:
    """Tests for Redis Cache settings validation errors from environment variables."""

    @pytest.mark.parametrize(
        ("environs"),
        [
            {},
            {"REDIS_URL": URL, "REDIS_HOST": "test_host"},
            {"REDIS_URL": "test://:test_password@test_host:1234/0"},
        ],
    )
    def test_redis_env_var_settings_validation_error(
        self,
        environs: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test Redis Cache raises CacheSettingsValidationError for bad env vars."""
        # Arrange
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        # Assert / Act
        with pytest.raises(
            CacheSettingsValidationError,
            match=r"Could not validate environment variables settings:\n",
        ):
            RedisCache(ttl=60)


class TestRedisCacheConstructorValidation:
    """Tests for Redis Cache constructor argument validation."""

    def test_ttl_zero_raises_value_error(self) -> None:
        """Test that ttl=0 raises ValueError."""
        with pytest.raises(ValueError, match="ttl must be positive"):
            RedisCache(url=URL, ttl=0)

    def test_ttl_negative_raises_value_error(self) -> None:
        """Test that ttl=-1 raises ValueError."""
        with pytest.raises(ValueError, match="ttl must be positive"):
            RedisCache(url=URL, ttl=-1)

    def test_explicit_url_bypasses_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that an explicit URL bypasses environment variable resolution."""
        # Arrange -- no REDIS_URL or REDIS_HOST set; env vars are absent
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_HOST", raising=False)

        # Act
        cache = RedisCache(url=URL, ttl=60)

        # Assert
        assert cache._url == URL


class TestCacheBackendRegistry:
    """Tests for cache backend registry."""

    def test_auto_register(self) -> None:
        """Test that RedisCache auto-registers in the cache backend registry."""
        # Arrange
        cache_backend_registry.reset()

        # Act
        RedisCache(url=URL, ttl=60)

        # Assert
        assert cache_backend_registry.is_loaded

        # Cleanup
        cache_backend_registry.reset()

    def test_auto_register_false(self) -> None:
        """Test that auto_register=False skips registration."""
        # Arrange
        cache_backend_registry.reset()

        # Act
        RedisCache(url=URL, ttl=60, auto_register=False)

        # Assert
        assert not cache_backend_registry.is_loaded

    def test_get_cache_backend(self) -> None:
        """Test get_cache_backend returns the registered backend."""
        # Arrange
        cache_backend_registry.reset()
        cache = RedisCache(url=URL, ttl=60)

        # Act
        result = get_cache_backend()

        # Assert
        assert result is cache

        # Cleanup
        cache_backend_registry.reset()

    def test_get_cache_backend_not_loaded(self) -> None:
        """Test get_cache_backend raises when no backend is registered."""
        # Arrange
        cache_backend_registry.reset()

        # Act / Assert
        with pytest.raises(BackendNotLoadedError):
            get_cache_backend()

    def test_cache_info_local_counters(self) -> None:
        """Test that cache_info returns local counters."""
        # Arrange
        cache = RedisCache(url=URL, ttl=60, auto_register=False)

        # Act
        info = cache.cache_info()

        # Assert
        assert info.hits == 0
        assert info.misses == 0
        assert info.maxsize == 0
        assert info.currsize == 0
        assert info.evictions == 0


class TestRedisCacheAsyncMethods:
    """Tests for RedisCache async methods using mocked Redis client."""

    async def test_get_hit(self) -> None:
        """Test get returns cached value and increments hits."""
        cache = RedisCache(url=URL, ttl=60, auto_register=False)
        cache._redis = MagicMock()
        cache._redis.get = AsyncMock(return_value=b"value")

        result = await cache.get("key")

        assert result == b"value"
        assert cache.cache_info().hits == 1
        assert cache.cache_info().misses == 0

    async def test_get_miss(self) -> None:
        """Test get returns default and increments misses."""
        cache = RedisCache(url=URL, ttl=60, auto_register=False)
        cache._redis = MagicMock()
        cache._redis.get = AsyncMock(return_value=None)

        result = await cache.get("key", "default")

        assert result == "default"
        assert cache.cache_info().misses == 1
        assert cache.cache_info().hits == 0

    async def test_set(self) -> None:
        """Test set calls redis.set with prefix and TTL."""
        cache = RedisCache(url=URL, ttl=30, prefix="p:", auto_register=False)
        cache._redis = MagicMock()
        cache._redis.set = AsyncMock()

        await cache.set("key", b"value")

        cache._redis.set.assert_awaited_once_with("p:key", b"value", ex=30)

    async def test_delete(self) -> None:
        """Test delete calls redis.delete with prefix."""
        cache = RedisCache(url=URL, ttl=60, prefix="p:", auto_register=False)
        cache._redis = MagicMock()
        cache._redis.delete = AsyncMock()

        await cache.delete("key")

        cache._redis.delete.assert_awaited_once_with("p:key")

    async def test_clear(self) -> None:
        """Test clear scans and deletes matching keys."""
        cache = RedisCache(url=URL, ttl=60, prefix="p:", auto_register=False)
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in [b"p:a", b"p:b"]:
                yield key

        mock_redis.scan_iter = mock_scan_iter
        cache._redis = mock_redis

        await cache.clear()

        mock_redis.delete.assert_awaited_once_with(b"p:a", b"p:b")

    async def test_clear_large_batch(self) -> None:
        """Test clear deletes in batches when keys exceed batch size."""
        cache = RedisCache(url=URL, ttl=60, prefix="p:", auto_register=False)
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()

        keys = [f"p:key{i}".encode() for i in range(1500)]

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in keys:
                yield key

        mock_redis.scan_iter = mock_scan_iter
        cache._redis = mock_redis

        await cache.clear()

        # First batch of 1000 + remaining 500
        expected_calls = 2
        assert mock_redis.delete.await_count == expected_calls

    async def test_context_manager(self) -> None:
        """Test async context manager calls aclose on exit."""
        cache = RedisCache(url=URL, ttl=60, auto_register=False)
        cache._redis = MagicMock()
        cache._redis.aclose = AsyncMock()

        async with cache:
            pass

        cache._redis.aclose.assert_awaited_once()

    async def test_get_with_prefix(self) -> None:
        """Test get prepends prefix to key."""
        cache = RedisCache(url=URL, ttl=60, prefix="ns:", auto_register=False)
        cache._redis = MagicMock()
        cache._redis.get = AsyncMock(return_value=b"v")

        await cache.get("k")

        cache._redis.get.assert_awaited_once_with("ns:k")
