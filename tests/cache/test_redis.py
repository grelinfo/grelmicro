"""Tests for RedisCacheAdapter."""

from collections.abc import AsyncIterator
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


def _build(prefix: str = "") -> tuple[RedisCacheAdapter, MagicMock]:
    """Return an adapter wired to a mocked Redis client via a provider."""
    provider = RedisProvider(URL)
    mock_client = MagicMock()
    provider._client = mock_client
    backend = RedisCacheAdapter(provider=provider, prefix=prefix)
    return backend, mock_client


class TestRedisCacheAdapterConstructor:
    """Tests for `RedisCacheAdapter` constructor behavior."""

    def test_explicit_provider_is_borrowed(self) -> None:
        """An explicit `provider=` is borrowed, not owned."""
        provider = RedisProvider(URL)

        backend = RedisCacheAdapter(provider=provider)

        assert backend.provider is provider
        assert backend._owns_provider is False

    def test_no_provider_builds_implicit_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without `provider=`, the adapter builds its own from env vars."""
        monkeypatch.setenv("REDIS_URL", URL)

        backend = RedisCacheAdapter()

        assert backend.provider.url == URL
        assert backend._owns_provider is True

    def test_env_prefix_reaches_implicit_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`env_prefix=` reaches the implicit provider."""
        monkeypatch.setenv("CACHE_REDIS_URL", URL)

        backend = RedisCacheAdapter(env_prefix="CACHE_REDIS_")

        assert backend.provider.url == URL
        assert backend.provider.env_prefix == "CACHE_REDIS_"

    def test_prefix_stored(self) -> None:
        """`prefix=` is stored on the adapter."""
        backend, _ = _build(prefix="myns:")

        assert backend._key_prefix == "myns:"


class TestRedisCacheAdapterAsyncMethods:
    """Tests for `RedisCacheAdapter` async methods using a mocked Redis client."""

    async def test_get_hit(self) -> None:
        """`get` returns the stored bytes when the key exists."""
        backend, client = _build()
        client.get = AsyncMock(return_value=b"value")

        result = await backend.get(key="mykey")

        assert result == b"value"
        client.get.assert_awaited_once_with("mykey")

    async def test_get_miss_returns_none(self) -> None:
        """`get` returns None for a missing key."""
        backend, client = _build()
        client.get = AsyncMock(return_value=None)

        result = await backend.get(key="missing")

        assert result is None

    async def test_get_with_prefix(self) -> None:
        """`get` prepends the configured prefix."""
        backend, client = _build(prefix="ns:")
        client.get = AsyncMock(return_value=b"v")

        await backend.get(key="k")

        client.get.assert_awaited_once_with("ns:k")

    async def test_set_passes_key_value_ttl(self) -> None:
        """`set` forwards the prefixed key, value, and TTL."""
        backend, client = _build(prefix="p:")
        client.set = AsyncMock()

        await backend.set(key="key", value=b"value", ttl=30)

        client.set.assert_awaited_once_with("p:key", b"value", px=30000)

    async def test_set_without_prefix(self) -> None:
        """`set` uses the bare key when no prefix is configured."""
        backend, client = _build()
        client.set = AsyncMock()

        await backend.set(key="bare", value=b"data", ttl=60)

        client.set.assert_awaited_once_with("bare", b"data", px=60000)

    async def test_set_float_ttl(self) -> None:
        """`set` passes fractional TTL values through to Redis."""
        backend, client = _build()
        client.set = AsyncMock()

        await backend.set(key="k", value=b"v", ttl=0.5)

        client.set.assert_awaited_once_with("k", b"v", px=500)

    async def test_delete(self) -> None:
        """`delete` runs the delete-with-tags script on the prefixed key."""
        backend, client = _build(prefix="p:")
        client.eval = AsyncMock()

        await backend.delete(key="key")

        # script, numkeys, value key, reverse-tag set.
        client.eval.assert_awaited_once_with(
            ANY, 2, "p:key", "p:cache:rtag:key"
        )

    async def test_delete_missing_key_is_no_op(self) -> None:
        """`delete` on a missing key does not raise."""
        backend, client = _build()
        client.eval = AsyncMock(return_value=1)

        await backend.delete(key="nonexistent")

        client.eval.assert_awaited_once_with(
            ANY, 2, "nonexistent", "cache:rtag:nonexistent"
        )

    async def test_clear(self) -> None:
        """`clear` deletes every key matching the configured prefix."""
        backend, client = _build(prefix="p:")
        client.delete = AsyncMock()

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in [b"p:a", b"p:b"]:
                yield key

        client.scan_iter = mock_scan_iter

        await backend.clear()

        client.delete.assert_awaited_once_with(b"p:a", b"p:b")

    async def test_clear_large_batch(self) -> None:
        """`clear` deletes in batches when keys exceed batch size."""
        backend, client = _build(prefix="p:")
        client.delete = AsyncMock()

        keys = [f"p:key{i}".encode() for i in range(1500)]

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            for key in keys:
                yield key

        client.scan_iter = mock_scan_iter

        await backend.clear()

        expected_batches = 2
        assert client.delete.await_count == expected_batches

    async def test_clear_empty_store(self) -> None:
        """`clear` on an empty store issues no delete calls."""
        backend, client = _build(prefix="p:")
        client.delete = AsyncMock()

        async def mock_scan_iter(*, match: str) -> AsyncIterator[bytes]:  # noqa: ARG001
            return
            yield  # make it an async generator

        client.scan_iter = mock_scan_iter

        await backend.clear()

        client.delete.assert_not_awaited()

    async def test_context_manager_closes_provider_client(self) -> None:
        """Owning the implicit provider, `__aexit__` closes the Redis client."""
        backend, client = _build()
        backend._owns_provider = True
        client.aclose = AsyncMock()

        async with backend:
            pass

        client.aclose.assert_awaited_once()

    async def test_context_manager_returns_self(self) -> None:
        """`__aenter__` returns the adapter."""
        backend, client = _build()
        client.aclose = AsyncMock()

        async with backend as entered:
            assert entered is backend
