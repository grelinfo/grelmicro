"""Tests for RedisCacheBackend."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from grelmicro._backends import BackendNotLoadedError
from grelmicro.cache import use_backend
from grelmicro.cache._backends import cache_backend_registry, get_cache_backend
from grelmicro.cache.errors import CacheSettingsValidationError
from grelmicro.cache.redis import RedisCacheBackend

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


class TestRedisCacheBackendEnvVarSettings:
    """Tests for RedisCacheBackend settings resolved from environment variables."""

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
        """Test that the Redis URL is resolved correctly from environment variables."""
        # Arrange
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        # Act
        backend = RedisCacheBackend()

        # Assert
        assert backend._url == expected_url


class TestRedisCacheBackendEnvVarValidationError:
    """Tests for RedisCacheBackend settings validation errors."""

    @pytest.mark.parametrize(
        "environs",
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
        """Test that invalid env var combinations raise CacheSettingsValidationError."""
        # Arrange
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        # Act / Assert
        with pytest.raises(
            CacheSettingsValidationError,
            match=r"Could not validate environment variables settings:\n",
        ):
            RedisCacheBackend()


class TestRedisCacheBackendConstructor:
    """Tests for RedisCacheBackend constructor with explicit arguments."""

    def test_explicit_url_bypasses_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that an explicit URL bypasses environment variable resolution."""
        # Arrange: no REDIS_URL or REDIS_HOST set
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_HOST", raising=False)

        # Act
        backend = RedisCacheBackend(URL)

        # Assert
        assert backend._url == URL

    def test_explicit_url_as_keyword(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that url can be passed as keyword argument."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_HOST", raising=False)

        backend = RedisCacheBackend(url=URL)

        assert backend._url == URL

    def test_prefix_stored(self) -> None:
        """Test that the prefix argument is stored on the backend."""
        backend = RedisCacheBackend(URL, prefix="myns:")

        assert backend._prefix == "myns:"

    def test_no_ttl_in_constructor(self) -> None:
        """Test that RedisCacheBackend has no ttl constructor parameter."""
        # TTL is now per-call; constructing without ttl must not raise.
        backend = RedisCacheBackend(URL)

        # The backend protocol exposes no ttl attribute.
        assert not hasattr(backend, "ttl")
        assert not hasattr(backend, "_ttl")


class TestCacheBackendRegistry:
    """Tests for cache backend registry integration."""

    def test_constructor_does_not_register(self) -> None:
        """Constructing RedisCacheBackend performs no registry writes."""
        cache_backend_registry.reset()

        RedisCacheBackend(URL)

        assert not cache_backend_registry.is_loaded

    def test_use_backend_registers(self) -> None:
        """`cache.use_backend` registers the backend as default."""
        cache_backend_registry.reset()
        backend = RedisCacheBackend(URL)

        with pytest.warns(DeprecationWarning, match="grelmicro.cache"):
            use_backend(backend)

        assert get_cache_backend() is backend

        cache_backend_registry.reset()

    def test_get_cache_backend_not_loaded_raises(self) -> None:
        """Test that get_cache_backend raises BackendNotLoadedError when empty."""
        cache_backend_registry.reset()

        with pytest.raises(BackendNotLoadedError):
            get_cache_backend()


class TestRedisCacheBackendAsyncMethods:
    """Tests for RedisCacheBackend async methods using a mocked Redis client."""

    async def test_get_hit(self) -> None:
        """Test that get returns the stored bytes when the key exists."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.get = AsyncMock(return_value=b"value")

        result = await backend.get(key="mykey")

        assert result == b"value"
        backend._redis.get.assert_awaited_once_with("mykey")

    async def test_get_miss_returns_none(self) -> None:
        """Test that get returns None when the key does not exist."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.get = AsyncMock(return_value=None)

        result = await backend.get(key="missing")

        assert result is None

    async def test_get_with_prefix(self) -> None:
        """Test that get prepends the configured prefix to the Redis key."""
        backend = RedisCacheBackend(URL, prefix="ns:")
        backend._redis = MagicMock()
        backend._redis.get = AsyncMock(return_value=b"v")

        await backend.get(key="k")

        backend._redis.get.assert_awaited_once_with("ns:k")

    async def test_set_passes_key_value_ttl(self) -> None:
        """Test that set calls redis.set with the prefixed key, value, and TTL."""
        backend = RedisCacheBackend(URL, prefix="p:")
        backend._redis = MagicMock()
        backend._redis.set = AsyncMock()

        await backend.set(key="key", value=b"value", ttl=30)

        backend._redis.set.assert_awaited_once_with("p:key", b"value", px=30000)

    async def test_set_without_prefix(self) -> None:
        """Test that set uses the bare key when no prefix is configured."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.set = AsyncMock()

        await backend.set(key="bare", value=b"data", ttl=60)

        backend._redis.set.assert_awaited_once_with("bare", b"data", px=60000)

    async def test_set_float_ttl(self) -> None:
        """Test that set passes fractional TTL values through to Redis."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.set = AsyncMock()

        await backend.set(key="k", value=b"v", ttl=0.5)

        backend._redis.set.assert_awaited_once_with("k", b"v", px=500)

    async def test_delete(self) -> None:
        """Test that delete calls redis.delete with the prefixed key."""
        backend = RedisCacheBackend(URL, prefix="p:")
        backend._redis = MagicMock()
        backend._redis.delete = AsyncMock()

        await backend.delete(key="key")

        backend._redis.delete.assert_awaited_once_with("p:key")

    async def test_delete_missing_key_is_no_op(self) -> None:
        """Test that delete on a missing key does not raise."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.delete = AsyncMock(return_value=0)

        # Should not raise even when Redis reports 0 deleted keys.
        await backend.delete(key="nonexistent")

        backend._redis.delete.assert_awaited_once_with("nonexistent")

    async def test_clear(self) -> None:
        """Test that clear scans and deletes all keys matching the prefix."""
        backend = RedisCacheBackend(URL, prefix="p:")
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in [b"p:a", b"p:b"]:
                yield key

        mock_redis.scan_iter = mock_scan_iter
        backend._redis = mock_redis

        await backend.clear()

        mock_redis.delete.assert_awaited_once_with(b"p:a", b"p:b")

    async def test_clear_large_batch(self) -> None:
        """Test that clear deletes in batches when the key count exceeds batch size."""
        backend = RedisCacheBackend(URL, prefix="p:")
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()

        keys = [f"p:key{i}".encode() for i in range(1500)]

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in keys:
                yield key

        mock_redis.scan_iter = mock_scan_iter
        backend._redis = mock_redis

        await backend.clear()

        # First batch of 1000 + remaining 500: two delete calls expected.
        expected_batches = 2
        assert mock_redis.delete.await_count == expected_batches

    async def test_clear_empty_store(self) -> None:
        """Test that clear on an empty store issues no delete calls."""
        backend = RedisCacheBackend(URL, prefix="p:")
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            return
            yield  # make it an async generator

        mock_redis.scan_iter = mock_scan_iter
        backend._redis = mock_redis

        await backend.clear()

        mock_redis.delete.assert_not_awaited()

    async def test_context_manager_closes_redis(self) -> None:
        """Test that the async context manager calls aclose on exit."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.aclose = AsyncMock()

        async with backend:
            pass

        backend._redis.aclose.assert_awaited_once()

    async def test_context_manager_returns_self(self) -> None:
        """Test that __aenter__ returns the backend instance itself."""
        backend = RedisCacheBackend(URL)
        backend._redis = MagicMock()
        backend._redis.aclose = AsyncMock()

        async with backend as entered:
            assert entered is backend
