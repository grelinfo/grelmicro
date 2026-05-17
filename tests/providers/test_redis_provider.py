"""Tests for `RedisProvider`."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from grelmicro import Grelmicro
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.providers.redis import (
    RedisConfig,
    RedisProvider,
    RedisProviderConfigError,
)
from grelmicro.resilience.redis import (
    RedisCircuitBreakerAdapter,
    RedisRateLimiterAdapter,
)
from grelmicro.sync.redis import RedisSyncAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


class TestConstruction:
    """Tests for `RedisProvider` construction forms."""

    def test_positional_url(self) -> None:
        """Positional URL is accepted."""
        provider = RedisProvider(URL)
        assert provider.url == URL

    def test_keyword_url(self) -> None:
        """Keyword `url=` is accepted."""
        provider = RedisProvider(url=URL)
        assert provider.url == URL

    def test_decomposed_kwargs(self) -> None:
        """Decomposed `host`, `port`, `db`, `password` are composed into a URL."""
        provider = RedisProvider(
            host="test_host",
            port=1234,
            db=0,
            password="test_password",  # noqa: S106
        )
        assert provider.url == URL

    def test_url_and_host_mutually_exclusive(self) -> None:
        """Passing both `url` and `host` raises."""
        with pytest.raises(RedisProviderConfigError, match="not both"):
            RedisProvider(url=URL, host="test_host")

    def test_env_load_disabled_requires_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `env_load=False` and no kwargs, construction raises."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_HOST", raising=False)

        with pytest.raises(RedisProviderConfigError):
            RedisProvider(env_load=False)

    @pytest.mark.parametrize(
        ("environs", "expected_url"),
        [
            ({"REDIS_URL": URL}, URL),
            (
                {
                    "REDIS_PASSWORD": "test_password",
                    "REDIS_HOST": "test_host",
                    "REDIS_PORT": "1234",
                    "REDIS_DB": "0",
                },
                URL,
            ),
            ({"REDIS_HOST": "test_host"}, "redis://test_host:6379/0"),
        ],
    )
    def test_env_driven(
        self,
        environs: dict[str, str],
        expected_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env vars under `REDIS_` populate the URL."""
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        provider = RedisProvider()

        assert provider.url == expected_url

    @pytest.mark.parametrize(
        "environs",
        [
            {},
            {"REDIS_URL": "test://h:1/0"},
            {"REDIS_URL": URL, "REDIS_HOST": "test_host"},
        ],
    )
    def test_env_validation_errors(
        self,
        environs: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid env combinations raise `RedisProviderConfigError`."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_HOST", raising=False)
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(RedisProviderConfigError):
            RedisProvider()

    def test_custom_env_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A custom `env_prefix` reads from a different env namespace."""
        monkeypatch.setenv("CACHE_REDIS_URL", URL)

        provider = RedisProvider(env_prefix="CACHE_REDIS_")

        assert provider.url == URL
        assert provider.env_prefix == "CACHE_REDIS_"


class TestFromConfig:
    """Tests for `RedisProvider.from_config`."""

    def test_from_config_uses_config_values(self) -> None:
        """`from_config` builds the URL from the config kwargs."""
        cfg = RedisConfig(
            host="cfg_host",
            port=4321,
            db=2,
            password="cfg_pw",  # noqa: S106
        )

        provider = RedisProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "4321" in provider.url

    def test_from_config_ignores_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`from_config` is authoritative and ignores the environment."""
        monkeypatch.setenv("REDIS_URL", "redis://env_host:9999/0")
        cfg = RedisConfig(host="cfg_host")

        provider = RedisProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "env_host" not in provider.url


class TestFromClient:
    """Tests for `RedisProvider.from_client`."""

    async def test_borrowed_client_not_closed(self) -> None:
        """`own=False` leaves the client alone on exit."""
        client = MagicMock()
        client.aclose = AsyncMock()

        async with RedisProvider.from_client(client) as provider:
            assert provider.client is client

        client.aclose.assert_not_awaited()

    async def test_owned_client_closed(self) -> None:
        """`own=True` closes the client on exit."""
        client = MagicMock()
        client.aclose = AsyncMock()

        async with RedisProvider.from_client(client, own=True):
            pass

        client.aclose.assert_awaited_once()


class TestBuilders:
    """Pure-sugar `.sync()` / `.cache()` builders."""

    def test_sync_builder_binds_provider(self) -> None:
        """`provider.sync()` returns an adapter borrowing the provider."""
        provider = RedisProvider(URL)

        adapter = provider.sync()

        assert isinstance(adapter, RedisSyncAdapter)
        assert adapter.provider is provider
        assert adapter._owns_provider is False

    def test_cache_builder_binds_provider(self) -> None:
        """`provider.cache()` returns an adapter borrowing the provider."""
        provider = RedisProvider(URL)

        adapter = provider.cache(prefix="ns:")

        assert isinstance(adapter, RedisCacheAdapter)
        assert adapter.provider is provider
        assert adapter._key_prefix == "ns:"

    def test_ratelimiter_builder_binds_provider(self) -> None:
        """`provider.ratelimiter()` returns an adapter borrowing the provider."""
        provider = RedisProvider(URL)

        adapter = provider.ratelimiter(prefix="rl:")

        assert isinstance(adapter, RedisRateLimiterAdapter)
        assert adapter.provider is provider
        assert adapter._prefix == "rl:"

    def test_breaker_factory(self) -> None:
        """`provider.breaker()` returns the canonical Redis adapter borrowing the provider."""
        provider = RedisProvider(URL)

        adapter = provider.breaker(prefix="cb:")

        assert isinstance(adapter, RedisCircuitBreakerAdapter)
        assert adapter.provider is provider
        assert adapter._prefix == "cb:"


class TestRebindProvider:
    """`_rebind_provider` swaps the bound provider on every Redis adapter."""

    def test_sync_adapter_rebind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RedisSyncAdapter rebinds to a new provider and re-registers scripts."""
        monkeypatch.setenv("REDIS_URL", URL)
        adapter = RedisSyncAdapter()
        assert adapter._owns_provider is True
        owned = RedisProvider(URL)

        adapter._rebind_provider(owned)

        assert adapter.provider is owned
        assert adapter._owns_provider is False

    def test_cache_adapter_rebind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RedisCacheAdapter rebinds to a new provider."""
        monkeypatch.setenv("REDIS_URL", URL)
        adapter = RedisCacheAdapter()
        assert adapter._owns_provider is True
        owned = RedisProvider(URL)

        adapter._rebind_provider(owned)

        assert adapter.provider is owned
        assert adapter._owns_provider is False

    async def test_rate_limiter_owned_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RedisRateLimiterAdapter opens and closes the provider it owns."""
        monkeypatch.setenv("REDIS_URL", URL)
        from grelmicro.resilience.redis import (  # noqa: PLC0415
            RedisRateLimiterAdapter,
        )

        backend = RedisRateLimiterAdapter()
        backend.provider._client = MagicMock(aclose=AsyncMock())

        async with backend:
            pass

        backend.provider._client.aclose.assert_awaited_once()

    async def test_circuit_breaker_owned_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RedisCircuitBreakerAdapter opens and closes the provider it owns."""
        monkeypatch.setenv("REDIS_URL", URL)

        backend = RedisCircuitBreakerAdapter()
        backend.provider._client = MagicMock(aclose=AsyncMock())

        async with backend:
            pass

        backend.provider._client.aclose.assert_awaited_once()

    def test_circuit_breaker_rebind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RedisCircuitBreakerAdapter rebinds to a new provider."""
        monkeypatch.setenv("REDIS_URL", URL)

        owned = RedisProvider(URL)
        backend = RedisCircuitBreakerAdapter()
        assert backend._owns_provider is True

        backend._rebind_provider(owned)

        assert backend.provider is owned
        assert backend._owns_provider is False

    def test_rate_limiter_rebind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RedisRateLimiterAdapter rebinds to a new provider."""
        monkeypatch.setenv("REDIS_URL", URL)
        from grelmicro.resilience.redis import (  # noqa: PLC0415
            RedisRateLimiterAdapter,
        )

        owned = RedisProvider(URL)
        backend = RedisRateLimiterAdapter()
        assert backend._owns_provider is True

        backend._rebind_provider(owned)

        assert backend.provider is owned
        assert backend._owns_provider is False


class TestSharingCache:
    """`Grelmicro` dedupes implicit providers by `(class, env_prefix)`."""

    async def test_two_adapters_same_env_prefix_share_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two adapters with the same default env_prefix share one provider."""
        monkeypatch.setenv("REDIS_URL", URL)

        sync_adapter = RedisSyncAdapter()
        cache_adapter = RedisCacheAdapter()
        assert sync_adapter.provider is not cache_adapter.provider

        from grelmicro.cache._component import Cache  # noqa: PLC0415
        from grelmicro.sync._component import Sync  # noqa: PLC0415

        for ad in (sync_adapter, cache_adapter):
            ad.provider._client = MagicMock(aclose=AsyncMock())

        micro = Grelmicro(uses=[Sync(sync_adapter), Cache(cache_adapter)])
        async with micro:
            assert sync_adapter.provider is cache_adapter.provider
            assert sync_adapter._owns_provider is True
            assert cache_adapter._owns_provider is False

    async def test_different_env_prefixes_keep_separate_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct env_prefixes keep distinct providers."""
        monkeypatch.setenv("CACHE_REDIS_URL", URL)
        monkeypatch.setenv("SESSION_REDIS_URL", URL)

        cache_adapter = RedisCacheAdapter(env_prefix="CACHE_REDIS_")
        sync_adapter = RedisSyncAdapter(env_prefix="SESSION_REDIS_")
        for ad in (cache_adapter, sync_adapter):
            ad.provider._client = MagicMock(aclose=AsyncMock())

        from grelmicro.cache._component import Cache  # noqa: PLC0415
        from grelmicro.sync._component import Sync  # noqa: PLC0415

        micro = Grelmicro(uses=[Cache(cache_adapter), Sync(sync_adapter)])
        async with micro:
            assert cache_adapter.provider is not sync_adapter.provider
