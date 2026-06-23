"""Tests for `ValkeyProvider`."""

from importlib.metadata import entry_points
from unittest.mock import AsyncMock, MagicMock

import pytest

from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.coordination.redis import (
    RedisLeaderElectionAdapter,
    RedisLockAdapter,
    RedisScheduleAdapter,
)
from grelmicro.providers.redis import RedisConfig, RedisProviderConfigError
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.resilience.circuitbreaker.redis import (
    RedisCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


class TestConstruction:
    """Tests for `ValkeyProvider` construction forms."""

    def test_positional_url(self) -> None:
        """Positional URL is accepted."""
        provider = ValkeyProvider(URL)
        assert provider.url == URL

    def test_keyword_url(self) -> None:
        """Keyword `url=` is accepted."""
        provider = ValkeyProvider(url=URL)
        assert provider.url == URL

    def test_decomposed_kwargs(self) -> None:
        """Decomposed `host`, `port`, `db`, `password` are composed into a URL."""
        provider = ValkeyProvider(
            host="test_host",
            port=1234,
            db=0,
            password="test_password",
        )
        assert provider.url == URL

    def test_url_and_host_mutually_exclusive(self) -> None:
        """Passing both `url` and `host` raises."""
        with pytest.raises(RedisProviderConfigError, match="not both"):
            ValkeyProvider(url=URL, host="test_host")

    def test_env_load_disabled_requires_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `env_load=False` and no kwargs, construction raises."""
        monkeypatch.delenv("VALKEY_URL", raising=False)
        monkeypatch.delenv("VALKEY_HOST", raising=False)

        with pytest.raises(RedisProviderConfigError):
            ValkeyProvider(env_load=False)

    @pytest.mark.parametrize(
        ("environs", "expected_url"),
        [
            ({"VALKEY_URL": URL}, URL),
            (
                {
                    "VALKEY_PASSWORD": "test_password",
                    "VALKEY_HOST": "test_host",
                    "VALKEY_PORT": "1234",
                    "VALKEY_DB": "0",
                },
                URL,
            ),
            ({"VALKEY_HOST": "test_host"}, "redis://test_host:6379/0"),
        ],
    )
    def test_env_driven(
        self,
        environs: dict[str, str],
        expected_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env vars under `VALKEY_` populate the URL."""
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        provider = ValkeyProvider()

        assert provider.url == expected_url

    @pytest.mark.parametrize(
        "environs",
        [
            {},
            {"VALKEY_URL": "test://h:1/0"},
            {"VALKEY_URL": URL, "VALKEY_HOST": "test_host"},
        ],
    )
    def test_env_validation_errors(
        self,
        environs: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid env combinations raise `RedisProviderConfigError`."""
        monkeypatch.delenv("VALKEY_URL", raising=False)
        monkeypatch.delenv("VALKEY_HOST", raising=False)
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(RedisProviderConfigError):
            ValkeyProvider()

    def test_default_env_prefix_is_valkey(self) -> None:
        """`ValkeyProvider` defaults to `VALKEY_` as its env prefix."""
        provider = ValkeyProvider(URL)
        assert provider.env_prefix == "VALKEY_"

    def test_custom_env_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A custom `env_prefix` reads from a different env namespace."""
        monkeypatch.setenv("CACHE_VALKEY_URL", URL)

        provider = ValkeyProvider(env_prefix="CACHE_VALKEY_")

        assert provider.url == URL
        assert provider.env_prefix == "CACHE_VALKEY_"

    def test_client_is_valkey_asyncio(self) -> None:
        """The underlying client comes from `valkey.asyncio`, not `redis.asyncio`."""
        from valkey.asyncio.client import Valkey  # noqa: PLC0415

        provider = ValkeyProvider(URL)

        assert isinstance(provider.client, Valkey)

    def test_is_subclass_of_redis_provider(self) -> None:
        """`ValkeyProvider` is a subclass of `RedisProvider`."""
        from grelmicro.providers.redis import RedisProvider  # noqa: PLC0415

        assert issubclass(ValkeyProvider, RedisProvider)


class TestFromConfig:
    """Tests for `ValkeyProvider.from_config`."""

    def test_from_config_uses_config_values(self) -> None:
        """`from_config` builds the URL from the config kwargs."""
        cfg = RedisConfig(
            host="cfg_host",
            port=4321,
            db=2,
            password="cfg_pw",
        )

        provider = ValkeyProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "4321" in provider.url

    def test_from_config_ignores_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`from_config` is authoritative and ignores the environment."""
        monkeypatch.setenv("VALKEY_URL", "redis://env_host:9999/0")
        cfg = RedisConfig(host="cfg_host")

        provider = ValkeyProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "env_host" not in provider.url

    def test_from_config_default_env_prefix_is_valkey(self) -> None:
        """`from_config` defaults env_prefix to `VALKEY_`."""
        cfg = RedisConfig(host="cfg_host")
        provider = ValkeyProvider.from_config(cfg)
        assert provider.env_prefix == "VALKEY_"


class TestFromClient:
    """Tests for `ValkeyProvider.from_client`."""

    async def test_borrowed_client_not_closed(self) -> None:
        """`own=False` leaves the client alone on exit."""
        client = MagicMock()
        client.aclose = AsyncMock()

        async with ValkeyProvider.from_client(client) as provider:
            assert provider.client is client

        client.aclose.assert_not_awaited()

    async def test_owned_client_closed(self) -> None:
        """`own=True` closes the client on exit."""
        client = MagicMock()
        client.aclose = AsyncMock()

        async with ValkeyProvider.from_client(client, own=True):
            pass

        client.aclose.assert_awaited_once()


class TestCheck:
    """`ValkeyProvider` inherits the Redis `PING` readiness probe."""

    async def test_check_pings(self) -> None:
        """`check` pings the client and returns None on success."""
        client = MagicMock()
        client.ping = AsyncMock()

        provider = ValkeyProvider.from_client(client)
        assert await provider.check() is None
        client.ping.assert_awaited_once()


class TestSafeUrl:
    """`safe_url` and `repr` must redact passwords."""

    def test_safe_url_redacts_password(self) -> None:
        """The password in the URL is replaced with `***`."""
        provider = ValkeyProvider(URL)
        assert provider.safe_url == "redis://:***@test_host:1234/0"

    def test_repr_never_exposes_password(self) -> None:
        """`repr()` uses the redacted URL form."""
        provider = ValkeyProvider(URL)
        assert "test_password" not in repr(provider)
        assert "***" in repr(provider)

    def test_repr_includes_class_name(self) -> None:
        """`repr()` names the class `ValkeyProvider`."""
        provider = ValkeyProvider(URL)
        assert repr(provider).startswith("ValkeyProvider(")


class TestBuilders:
    """Pure-sugar `.lock()` / `.cache()` builders."""

    def test_lock_builder_binds_provider(self) -> None:
        """`provider.lock()` returns a `RedisLockAdapter` borrowing the provider."""
        provider = ValkeyProvider(URL)

        adapter = provider.lock()

        assert isinstance(adapter, RedisLockAdapter)
        assert adapter.provider is provider
        assert adapter._owns_provider is False

    def test_cache_builder_binds_provider(self) -> None:
        """`provider.cache()` returns a `RedisCacheAdapter` borrowing the provider."""
        provider = ValkeyProvider(URL)

        adapter = provider.cache(prefix="ns:")

        assert isinstance(adapter, RedisCacheAdapter)
        assert adapter.provider is provider
        assert adapter._key_prefix == "ns:"

    def test_ratelimiter_builder_binds_provider(self) -> None:
        """`provider.ratelimiter()` returns a `RedisRateLimiterAdapter` borrowing the provider."""
        provider = ValkeyProvider(URL)

        adapter = provider.ratelimiter(prefix="rl:")

        assert isinstance(adapter, RedisRateLimiterAdapter)
        assert adapter.provider is provider
        assert adapter._prefix == "rl:"

    def test_circuitbreaker_factory(self) -> None:
        """`provider.circuitbreaker()` returns a `RedisCircuitBreakerAdapter`."""
        provider = ValkeyProvider(URL)

        adapter = provider.circuitbreaker(prefix="cb:")

        assert isinstance(adapter, RedisCircuitBreakerAdapter)
        assert adapter.provider is provider
        assert adapter._prefix == "cb:"

    def test_leaderelection_builder_binds_provider(self) -> None:
        """`provider.leaderelection()` returns a `RedisLeaderElectionAdapter`."""
        provider = ValkeyProvider(URL)

        adapter = provider.leaderelection()

        assert isinstance(adapter, RedisLeaderElectionAdapter)
        assert adapter._provider is provider
        assert adapter._owns_provider is False

    def test_schedule_builder_binds_provider(self) -> None:
        """`provider.schedule()` returns a `RedisScheduleAdapter` borrowing the provider."""
        provider = ValkeyProvider(URL)

        adapter = provider.schedule()

        assert isinstance(adapter, RedisScheduleAdapter)
        assert adapter._provider is provider
        assert adapter._owns_provider is False


class TestSentinelAndCluster:
    """`ValkeyProvider` supports the sentinel and cluster URL schemes."""

    def test_sentinel_url_builds_valkey_master_proxy(self) -> None:
        """A `redis+sentinel://` URL opens a Valkey Sentinel client."""
        from valkey.asyncio.client import Valkey  # noqa: PLC0415
        from valkey.asyncio.sentinel import Sentinel  # noqa: PLC0415

        provider = ValkeyProvider(
            "redis+sentinel://h1:26379,h2:26379/mymaster/0"
        )

        assert isinstance(provider.client, Valkey)
        assert isinstance(provider._sentinel, Sentinel)
        assert provider.is_cluster is False

    def test_cluster_url_builds_valkey_cluster(self) -> None:
        """A `redis+cluster://` URL opens a `ValkeyCluster` client."""
        from valkey.asyncio.cluster import ValkeyCluster  # noqa: PLC0415

        provider = ValkeyProvider("redis+cluster://h1:6379,h2:6379")

        assert isinstance(provider.client, ValkeyCluster)
        assert provider.is_cluster is True

    def test_sentinel_factory(self) -> None:
        """`ValkeyProvider.sentinel(...)` composes the URL and client."""
        from valkey.asyncio.client import Valkey  # noqa: PLC0415

        provider = ValkeyProvider.sentinel(
            sentinels=[("a", 26379)], service_name="svc", db=1
        )

        assert provider.url == "redis+sentinel://a:26379/svc/1"
        assert isinstance(provider.client, Valkey)

    def test_cluster_factory(self) -> None:
        """`ValkeyProvider.cluster(...)` composes the URL and client."""
        from valkey.asyncio.cluster import ValkeyCluster  # noqa: PLC0415

        provider = ValkeyProvider.cluster(nodes=[("a", 6379)])

        assert provider.url == "redis+cluster://a:6379"
        assert isinstance(provider.client, ValkeyCluster)
        assert provider.is_cluster is True

    def test_cluster_hash_tag_guard(self) -> None:
        """The cache guard fires for a tag-less prefix on a Valkey cluster."""
        provider = ValkeyProvider("redis+cluster://h1:6379,h2:6379")

        with pytest.raises(ValueError, match="hash tag"):
            RedisCacheAdapter(provider=provider, prefix="cache")

        adapter = RedisCacheAdapter(provider=provider, prefix="{myapp}cache")
        assert adapter.provider is provider


class TestEntryPoint:
    """Entry-point discovery resolves the `valkey` short name."""

    def test_entry_point_resolves_valkey_provider(self) -> None:
        """The `valkey` entry point resolves to `ValkeyProvider`."""
        eps = entry_points(group="grelmicro.providers")
        names = {ep.name: ep for ep in eps}
        assert "valkey" in names
        loaded = names["valkey"].load()
        assert loaded is ValkeyProvider
