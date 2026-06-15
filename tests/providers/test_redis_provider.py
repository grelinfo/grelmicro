"""Tests for `RedisProvider`."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.asyncio.client import Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.sentinel import Sentinel

from grelmicro import Grelmicro
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.providers.redis import (
    RedisConfig,
    RedisProvider,
    RedisProviderConfigError,
)
from grelmicro.resilience.circuitbreaker.redis import (
    RedisCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"
_TWO_HOSTS = 2
_DEFAULT_SENTINEL_PORT = 26379


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
            password="test_password",
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
            password="cfg_pw",
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


class TestCheck:
    """Tests for `RedisProvider.check` readiness probe."""

    async def test_check_pings(self) -> None:
        """`check` pings the client and returns None on success."""
        client = MagicMock()
        client.ping = AsyncMock()

        provider = RedisProvider.from_client(client)
        assert await provider.check() is None
        client.ping.assert_awaited_once()

    async def test_check_propagates_failure(self) -> None:
        """A ping failure surfaces from `check`."""
        client = MagicMock()
        client.ping = AsyncMock(side_effect=ConnectionError("down"))

        provider = RedisProvider.from_client(client)
        with pytest.raises(ConnectionError):
            await provider.check()


class TestSafeUrl:
    """`safe_url` and `repr` must redact passwords."""

    def test_safe_url_redacts_password(self) -> None:
        """The password in the URL is replaced with `***`."""
        provider = RedisProvider(URL)
        assert provider.safe_url == "redis://:***@test_host:1234/0"

    def test_safe_url_passthrough_when_no_password(self) -> None:
        """URLs without a password are returned unchanged."""
        provider = RedisProvider("redis://test_host:6379/0")
        assert provider.safe_url == "redis://test_host:6379/0"

    def test_safe_url_empty_string_returned_as_is(self) -> None:
        """An empty URL (e.g. `from_client` providers) is returned unchanged."""
        from grelmicro.providers.redis import _redact_url  # noqa: PLC0415

        assert _redact_url("") == ""

    def test_safe_url_invalid_url_returned_as_is(self) -> None:
        """A non-URL string with no userinfo falls back to the input."""
        from grelmicro.providers.redis import _redact_url  # noqa: PLC0415

        assert _redact_url("not-a-valid-url") == "not-a-valid-url"

    def test_safe_url_malformed_with_password_still_redacted(self) -> None:
        """A malformed URL that still contains a password is redacted by regex."""
        from grelmicro.providers.redis import _redact_url  # noqa: PLC0415

        assert (
            _redact_url("redis://:secret@bad host:6379/0")
            == "redis://:***@bad host:6379/0"
        )

    def test_safe_url_query_credentials_redacted(self) -> None:
        """Credential-like query params (password, token, ...) are redacted."""
        from grelmicro.providers.redis import _redact_url  # noqa: PLC0415

        assert (
            _redact_url("redis://host/0?password=secret&db=1")
            == "redis://host/0?password=***&db=1"
        )
        assert (
            _redact_url("redis://host/0?TOKEN=abc")
            == "redis://host/0?TOKEN=***"
        )

    def test_safe_url_query_without_credentials_passthrough(self) -> None:
        """A URL with a query but no credential keys is returned unchanged."""
        from grelmicro.providers.redis import _redact_url  # noqa: PLC0415

        assert _redact_url("redis://host/0?foo=bar") == "redis://host/0?foo=bar"

    def test_repr_never_exposes_password(self) -> None:
        """`repr()` uses the redacted URL form."""
        provider = RedisProvider(URL)
        assert "test_password" not in repr(provider)
        assert "***" in repr(provider)


class TestBuilders:
    """Pure-sugar `.lock()` / `.cache()` builders."""

    def test_lock_builder_binds_provider(self) -> None:
        """`provider.lock()` returns an adapter borrowing the provider."""
        provider = RedisProvider(URL)

        adapter = provider.lock()

        assert isinstance(adapter, RedisLockAdapter)
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

    def test_circuitbreaker_factory(self) -> None:
        """`provider.circuitbreaker()` returns the matching Redis adapter."""
        provider = RedisProvider(URL)

        adapter = provider.circuitbreaker(prefix="cb:")

        assert isinstance(adapter, RedisCircuitBreakerAdapter)
        assert adapter.provider is provider
        assert adapter._prefix == "cb:"

    def test_leaderelection_builder_binds_provider(self) -> None:
        """`provider.leaderelection()` returns a backend borrowing it."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisLeaderElectionBackend,
        )

        provider = RedisProvider(URL)

        adapter = provider.leaderelection()

        assert isinstance(adapter, RedisLeaderElectionBackend)
        assert adapter.provider is provider

    def test_schedule_builder_binds_provider(self) -> None:
        """`provider.schedule()` returns a `RedisScheduleAdapter`."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisScheduleAdapter,
        )

        provider = RedisProvider(URL)

        adapter = provider.schedule()

        assert isinstance(adapter, RedisScheduleAdapter)
        assert adapter.provider is provider


class TestRebindProvider:
    """`_rebind_provider` swaps the bound provider on every Redis adapter."""

    def test_sync_adapter_rebind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RedisLockAdapter rebinds to a new provider and re-registers scripts."""
        monkeypatch.setenv("REDIS_URL", URL)
        adapter = RedisLockAdapter()
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
        from grelmicro.resilience.ratelimiter.redis import (  # noqa: PLC0415
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
        from grelmicro.resilience.ratelimiter.redis import (  # noqa: PLC0415
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

        sync_adapter = RedisLockAdapter()
        cache_adapter = RedisCacheAdapter()
        assert sync_adapter.provider is not cache_adapter.provider

        from grelmicro.cache._component import Cache  # noqa: PLC0415
        from grelmicro.coordination._component import (  # noqa: PLC0415
            Coordination,
        )

        for ad in (sync_adapter, cache_adapter):
            ad.provider._client = MagicMock(aclose=AsyncMock())

        micro = Grelmicro(
            uses=[Coordination(lock=sync_adapter), Cache(cache_adapter)]
        )
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
        sync_adapter = RedisLockAdapter(env_prefix="SESSION_REDIS_")
        for ad in (cache_adapter, sync_adapter):
            ad.provider._client = MagicMock(aclose=AsyncMock())

        from grelmicro.cache._component import Cache  # noqa: PLC0415
        from grelmicro.coordination._component import (  # noqa: PLC0415
            Coordination,
        )

        micro = Grelmicro(
            uses=[Cache(cache_adapter), Coordination(lock=sync_adapter)]
        )
        async with micro:
            assert cache_adapter.provider is not sync_adapter.provider


class TestSentinelUrl:
    """`redis+sentinel://` URLs build a Sentinel master proxy."""

    def test_sentinel_url_builds_master_proxy(self) -> None:
        """The scheme opens a Sentinel and returns a plain `Redis` client."""
        provider = RedisProvider(
            "redis+sentinel://h1:26379,h2:26379/mymaster/0"
        )

        assert isinstance(provider.client, Redis)
        assert not isinstance(provider.client, RedisCluster)
        assert provider.is_cluster is False
        assert isinstance(provider._sentinel, Sentinel)
        assert [str(s) for s in provider._sentinel.sentinels] != []
        assert sum(1 for _ in provider._sentinel.sentinels) == _TWO_HOSTS

    def test_sentinel_url_default_port(self) -> None:
        """Hosts without a port default to the Sentinel port."""
        provider = RedisProvider("redis+sentinel://h1,h2/mymaster")

        assert isinstance(provider._sentinel, Sentinel)
        ports = {
            s.connection_pool.connection_kwargs["port"]
            for s in provider._sentinel.sentinels
        }
        assert ports == {_DEFAULT_SENTINEL_PORT}

    def test_sentinel_url_requires_service_name(self) -> None:
        """A sentinel URL with no path segment raises."""
        with pytest.raises(RedisProviderConfigError, match="master service"):
            RedisProvider("redis+sentinel://h1:26379,h2:26379")

    def test_sentinel_url_invalid_db(self) -> None:
        """A non-integer db segment raises."""
        with pytest.raises(RedisProviderConfigError, match="database index"):
            RedisProvider("redis+sentinel://h1:26379/mymaster/abc")

    def test_sentinel_url_no_host(self) -> None:
        """An authority with an empty host entry raises."""
        with pytest.raises(RedisProviderConfigError):
            RedisProvider("redis+sentinel://:26379/mymaster")

    def test_sentinel_url_safe_redacts_password(self) -> None:
        """`safe_url` redacts the multi-host userinfo password."""
        provider = RedisProvider(
            "redis+sentinel://:secret@h1:26379,h2:26379/mymaster/0"
        )

        assert provider.safe_url == (
            "redis+sentinel://:***@h1:26379,h2:26379/mymaster/0"
        )
        assert "secret" not in repr(provider)

    def test_sentinel_url_skips_empty_authority_item(self) -> None:
        """A trailing comma in the authority is ignored."""
        provider = RedisProvider("redis+sentinel://h1:26379,/mymaster")

        assert isinstance(provider._sentinel, Sentinel)
        assert sum(1 for _ in provider._sentinel.sentinels) == 1

    def test_cluster_url_with_no_hosts_raises(self) -> None:
        """An authority with only separators raises."""
        with pytest.raises(RedisProviderConfigError, match="at least one host"):
            RedisProvider("redis+cluster://,")

    async def test_sentinel_provider_closes_sentinels_on_exit(self) -> None:
        """Owned sentinel providers close every Sentinel connection on exit."""
        provider = RedisProvider.sentinel(
            sentinels=[("a", 26379), ("b", 26379)], service_name="svc"
        )
        provider._client = MagicMock(aclose=AsyncMock())
        sentinel_mocks = [MagicMock(aclose=AsyncMock()) for _ in range(2)]
        provider._sentinel = MagicMock(sentinels=sentinel_mocks)

        async with provider:
            pass

        provider._client.aclose.assert_awaited_once()
        for sentinel in sentinel_mocks:
            sentinel.aclose.assert_awaited_once()


class TestClusterUrl:
    """`redis+cluster://` URLs build a `RedisCluster` client."""

    def test_cluster_url_builds_cluster_client(self) -> None:
        """The scheme returns a `RedisCluster` and flags `is_cluster`."""
        provider = RedisProvider("redis+cluster://h1:6379,h2:6379")

        assert isinstance(provider.client, RedisCluster)
        assert provider.is_cluster is True

    def test_cluster_url_safe_redacts_password(self) -> None:
        """`safe_url` redacts the multi-host userinfo password."""
        provider = RedisProvider("redis+cluster://:secret@h1:6379,h2:6379")

        assert provider.safe_url == "redis+cluster://:***@h1:6379,h2:6379"
        assert "secret" not in repr(provider)


class TestSentinelFactory:
    """`RedisProvider.sentinel(...)` composes the URL and client."""

    def test_factory_composes_url_and_client(self) -> None:
        """The factory builds the URL and a Sentinel master proxy."""
        provider = RedisProvider.sentinel(
            sentinels=[("a", 26379), ("b", 26379)],
            service_name="svc",
            db=2,
            password="secret",
        )

        assert provider.url == (
            "redis+sentinel://:secret@a:26379,b:26379/svc/2"
        )
        assert provider.safe_url == (
            "redis+sentinel://:***@a:26379,b:26379/svc/2"
        )
        assert isinstance(provider.client, Redis)
        assert isinstance(provider._sentinel, Sentinel)

    def test_factory_passes_sentinel_kwargs(self) -> None:
        """`sentinel_kwargs` reaches the Sentinel connections."""
        provider = RedisProvider.sentinel(
            sentinels=[("a", 26379)],
            service_name="svc",
            sentinel_kwargs={"password": "sentinel_pw"},
        )

        assert isinstance(provider._sentinel, Sentinel)
        assert provider._sentinel.sentinel_kwargs == {"password": "sentinel_pw"}


class TestClusterFactory:
    """`RedisProvider.cluster(...)` composes the URL and client."""

    def test_factory_composes_url_and_client(self) -> None:
        """The factory builds the URL and a `RedisCluster` client."""
        provider = RedisProvider.cluster(
            nodes=[("a", 6379), ("b", 6379)],
            password="secret",
        )

        assert provider.url == "redis+cluster://:secret@a:6379,b:6379"
        assert provider.safe_url == "redis+cluster://:***@a:6379,b:6379"
        assert isinstance(provider.client, RedisCluster)
        assert provider.is_cluster is True


class TestClusterHashTagGuard:
    """Multi-key adapters demand a hash-tag prefix on Cluster."""

    @pytest.fixture
    def cluster_provider(self) -> RedisProvider:
        """Return a provider whose client is a `RedisCluster`."""
        return RedisProvider("redis+cluster://h1:6379,h2:6379")

    def test_cache_without_hash_tag_raises(
        self, cluster_provider: RedisProvider
    ) -> None:
        """The cache adapter rejects a tag-less prefix on Cluster."""
        with pytest.raises(ValueError, match="hash tag"):
            RedisCacheAdapter(provider=cluster_provider, prefix="cache")

    def test_cache_with_hash_tag_passes(
        self, cluster_provider: RedisProvider
    ) -> None:
        """A prefix carrying a hash tag is accepted on Cluster."""
        adapter = RedisCacheAdapter(
            provider=cluster_provider, prefix="{myapp}cache"
        )

        assert adapter.provider is cluster_provider

    def test_lock_without_hash_tag_raises(
        self, cluster_provider: RedisProvider
    ) -> None:
        """The lock adapter rejects a tag-less prefix on Cluster."""
        with pytest.raises(ValueError, match="hash tag"):
            RedisLockAdapter(provider=cluster_provider, prefix="lock:")

    def test_lock_with_hash_tag_passes(
        self, cluster_provider: RedisProvider
    ) -> None:
        """A prefix carrying a hash tag is accepted on Cluster."""
        adapter = RedisLockAdapter(provider=cluster_provider, prefix="{app}")

        assert adapter.provider is cluster_provider

    def test_standalone_needs_no_hash_tag(self) -> None:
        """Standalone clients skip the guard entirely."""
        provider = RedisProvider(URL)

        cache = RedisCacheAdapter(provider=provider, prefix="cache")
        lock = RedisLockAdapter(provider=provider, prefix="lock:")

        assert cache.provider is provider
        assert lock.provider is provider
