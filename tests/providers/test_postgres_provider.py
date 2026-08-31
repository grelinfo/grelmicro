"""Tests for `PostgresProvider`."""

import contextlib
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from grelmicro import Grelmicro
from grelmicro.cache.postgres import PostgresCacheAdapter
from grelmicro.coordination.postgres import PostgresLockAdapter
from grelmicro.errors import SettingsValidationError
from grelmicro.providers._base import Provider
from grelmicro.providers.postgres import (
    PostgresConfig,
    PostgresProvider,
)
from grelmicro.resilience.circuitbreaker.postgres import (
    PostgresCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.postgres import PostgresRateLimiterAdapter
from tests._postgres import mock_pool as make_pool

try:
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        create_async_engine,
    )
except ImportError:  # pragma: no cover
    HAS_SQLALCHEMY = False
else:
    HAS_SQLALCHEMY = True

requires_sqlalchemy = pytest.mark.skipif(
    not HAS_SQLALCHEMY, reason="sqlalchemy is not installed"
)

pytestmark = [pytest.mark.timeout(1)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"
ASYNCPG_URL = (
    "postgresql+asyncpg://test_user:test_password@test_host:1234/test_db"
)


class TestConstruction:
    """Tests for `PostgresProvider` construction forms."""

    def test_positional_url(self) -> None:
        """Positional URL is accepted."""
        provider = PostgresProvider(URL)
        assert provider.url == URL

    def test_keyword_url(self) -> None:
        """Keyword `url=` is accepted."""
        provider = PostgresProvider(url=URL)
        assert provider.url == URL

    def test_decomposed_kwargs(self) -> None:
        """Decomposed kwargs are composed into a URL."""
        provider = PostgresProvider(
            host="test_host",
            port=1234,
            database="test_db",
            user="test_user",
            password="test_password",
        )
        assert provider.url == URL

    def test_command_timeout_defaults_to_none(self) -> None:
        """No command timeout is set by default."""
        assert PostgresProvider(URL).command_timeout is None

    def test_command_timeout_kwarg(self) -> None:
        """The `command_timeout` kwarg is stored."""
        assert PostgresProvider(URL, command_timeout=5).command_timeout == 5  # noqa: PLR2004

    def test_command_timeout_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`command_timeout` resolves from `POSTGRES_COMMAND_TIMEOUT`."""
        monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT", "3")
        assert PostgresProvider(URL).command_timeout == 3  # noqa: PLR2004

    def test_command_timeout_kwarg_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kwarg overrides the environment."""
        monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT", "3")
        assert PostgresProvider(URL, command_timeout=1).command_timeout == 1

    def test_explicit_url_ignores_unrelated_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit URL is not broken by a stray unrelated env URL."""
        monkeypatch.setenv("POSTGRES_URL", "not-a-postgres-url")
        provider = PostgresProvider(URL)
        assert provider.url == URL
        assert provider.command_timeout is None

    def test_invalid_command_timeout_env_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric `POSTGRES_COMMAND_TIMEOUT` raises."""
        monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT", "abc")
        with pytest.raises(SettingsValidationError):
            PostgresProvider(URL)

    def test_url_and_host_mutually_exclusive(self) -> None:
        """Passing both `url` and `host` raises."""
        with pytest.raises(SettingsValidationError, match="not both"):
            PostgresProvider(url=URL, host="test_host")

    def test_env_load_disabled_requires_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `env_load=False` and no kwargs, construction raises."""
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.delenv("POSTGRES_HOST", raising=False)

        with pytest.raises(SettingsValidationError):
            PostgresProvider(env_load=False)

    @pytest.mark.parametrize(
        ("environs", "expected_url"),
        [
            ({"POSTGRES_URL": URL}, URL),
            (
                {
                    "POSTGRES_USER": "test_user",
                    "POSTGRES_PASSWORD": "test_password",
                    "POSTGRES_HOST": "test_host",
                    "POSTGRES_PORT": "1234",
                    "POSTGRES_DB": "test_db",
                },
                URL,
            ),
            (
                {"POSTGRES_HOST": "test_host"},
                "postgresql://test_host:5432",
            ),
        ],
    )
    def test_env_driven(
        self,
        environs: dict[str, str],
        expected_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env vars under `POSTGRES_` populate the URL."""
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        provider = PostgresProvider()

        assert provider.url == expected_url

    @pytest.mark.parametrize(
        ("environs", "expected_db"),
        [
            ({"POSTGRES_DB": "from_db"}, "from_db"),
            ({"POSTGRES_DATABASE": "from_database"}, "from_database"),
            ({"POSTGRES_DB": "wins", "POSTGRES_DATABASE": "loses"}, "wins"),
        ],
    )
    def test_env_database_name_aliases(
        self,
        environs: dict[str, str],
        expected_db: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The database name reads from `POSTGRES_DB` or `POSTGRES_DATABASE`.

        `DB` matches the `postgres` Docker image convention and wins when
        both are set.
        """
        monkeypatch.delenv("POSTGRES_DB", raising=False)
        monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "test_host")
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        provider = PostgresProvider()

        assert str(provider.url).endswith(f"/{expected_db}")

    @pytest.mark.parametrize(
        "environs",
        [
            {},
            {"POSTGRES_URL": "test://h:1/0"},
            {"POSTGRES_URL": URL, "POSTGRES_HOST": "test_host"},
        ],
    )
    def test_env_validation_errors(
        self,
        environs: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid env combinations raise `SettingsValidationError`."""
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.delenv("POSTGRES_HOST", raising=False)
        for key, value in environs.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(SettingsValidationError):
            PostgresProvider()

    def test_custom_env_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A custom `env_prefix` reads from a different env namespace."""
        monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

        provider = PostgresProvider(env_prefix="WRITE_POSTGRES_")

        assert provider.url == URL
        assert provider.env_prefix == "WRITE_POSTGRES_"


class TestFromConfig:
    """Tests for `PostgresProvider.from_config`."""

    def test_from_config_uses_config_values(self) -> None:
        """`from_config` builds the URL from the config kwargs."""
        cfg = PostgresConfig(
            host="cfg_host",
            port=4321,
            database="cfg_db",
            user="cfg_user",
            password="cfg_pw",
        )

        provider = PostgresProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "4321" in provider.url

    def test_from_config_ignores_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`from_config` is authoritative and ignores the environment."""
        monkeypatch.setenv("POSTGRES_URL", "postgresql://env_host:9999/env_db")
        cfg = PostgresConfig(host="cfg_host")

        provider = PostgresProvider.from_config(cfg)

        assert "cfg_host" in provider.url
        assert "env_host" not in provider.url


class TestFromClient:
    """Tests for `PostgresProvider.from_client`."""

    async def test_borrowed_pool_not_closed(self) -> None:
        """`own=False` leaves the pool alone on exit."""
        pool = MagicMock()
        pool.close = AsyncMock()

        async with PostgresProvider.from_client(pool) as provider:
            assert provider.client is pool

        pool.close.assert_not_awaited()

    async def test_owned_pool_closed(self) -> None:
        """`own=True` closes the pool on exit."""
        pool = MagicMock()
        pool.close = AsyncMock()

        async with PostgresProvider.from_client(pool, own=True):
            pass

        pool.close.assert_awaited_once()


@requires_sqlalchemy
class TestFromEngine:
    """Tests for `PostgresProvider.from_engine` argument validation."""

    def test_async_engine_accepted(self) -> None:
        """A `postgresql+asyncpg` engine builds a provider."""
        engine = create_async_engine(ASYNCPG_URL)

        provider = PostgresProvider.from_engine(engine)

        assert provider.url == ASYNCPG_URL
        assert provider.safe_url == (
            "postgresql+asyncpg://test_user:***@test_host:1234/test_db"
        )

    def test_session_refused(self) -> None:
        """An `AsyncSession` carries the caller's transaction, so it is refused."""
        session: Any = AsyncSession(create_async_engine(ASYNCPG_URL))

        with pytest.raises(SettingsValidationError, match="AsyncSession"):
            PostgresProvider.from_engine(session)

    def test_connection_refused(self) -> None:
        """An `AsyncConnection` is refused for the same reason."""
        connection: Any = AsyncConnection(create_async_engine(ASYNCPG_URL))

        with pytest.raises(SettingsValidationError, match="AsyncConnection"):
            PostgresProvider.from_engine(connection)

    def test_sync_engine_refused(self) -> None:
        """A sync `Engine` cannot serve an asyncpg connection."""
        engine: Any = create_engine("sqlite://")

        with pytest.raises(SettingsValidationError, match="AsyncEngine"):
            PostgresProvider.from_engine(engine)

    def test_non_engine_refused(self) -> None:
        """Anything else is refused by type, naming what arrived."""
        url: Any = "postgresql+asyncpg://host/db"

        with pytest.raises(SettingsValidationError, match="got str"):
            PostgresProvider.from_engine(url)

    def test_other_backend_refused(self) -> None:
        """A non-Postgres engine is refused by backend."""
        engine: Any = create_async_engine("sqlite+aiosqlite://")

        with pytest.raises(SettingsValidationError, match="backend should be"):
            PostgresProvider.from_engine(engine)

    def test_refusal_never_quotes_the_url(self) -> None:
        """A rejected engine never carries its URL into the message."""
        session: Any = AsyncSession(
            create_async_engine("sqlite+aiosqlite:///file.db")
        )

        with pytest.raises(SettingsValidationError) as error:
            PostgresProvider.from_engine(session)

        assert "file.db" not in str(error.value)


class TestCheck:
    """Tests for `PostgresProvider.check` readiness probe."""

    async def test_check_selects_one(self) -> None:
        """`check` runs `SELECT 1` on the pool and returns None."""
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=1)

        provider = PostgresProvider.from_client(pool)
        assert await provider.check() is None
        pool.fetchval.assert_awaited_once_with("SELECT 1")

    async def test_check_propagates_failure(self) -> None:
        """A query failure surfaces from `check`."""
        pool = MagicMock()
        pool.fetchval = AsyncMock(side_effect=ConnectionError("down"))

        provider = PostgresProvider.from_client(pool)
        with pytest.raises(ConnectionError):
            await provider.check()


class TestSafeUrl:
    """`safe_url` and `repr` must redact passwords."""

    def test_safe_url_redacts_password(self) -> None:
        """The password in the URL is replaced with `***`."""
        provider = PostgresProvider(URL)
        assert provider.safe_url == (
            "postgresql://test_user:***@test_host:1234/test_db"
        )

    def test_safe_url_passthrough_when_no_password(self) -> None:
        """URLs without a password are returned unchanged."""
        provider = PostgresProvider("postgresql://test_host:5432/app")
        assert provider.safe_url == "postgresql://test_host:5432/app"

    def test_safe_url_empty_string_returned_as_is(self) -> None:
        """An empty URL (e.g. `from_client` providers) is returned unchanged."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert redact_url("", multi_host=True) == ""

    def test_safe_url_invalid_url_returned_as_is(self) -> None:
        """A non-URL string with no userinfo falls back to the input."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url("not-a-valid-url", multi_host=True) == "not-a-valid-url"
        )

    def test_safe_url_malformed_with_password_still_redacted(self) -> None:
        """A malformed DSN that still contains a password is redacted by regex."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url("postgresql://u:p@bad host/db", multi_host=True)
            == "postgresql://u:***@bad host/db"
        )

    def test_safe_url_query_credentials_redacted(self) -> None:
        """Credential-like query params (password, token, ...) are redacted."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url(
                "postgresql://host/db?password=secret&sslmode=require",
                multi_host=True,
            )
            == "postgresql://host/db?password=***&sslmode=require"
        )

    def test_safe_url_query_without_credentials_passthrough(self) -> None:
        """A DSN with a query but no credential keys is returned unchanged."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url("postgresql://host/db?sslmode=require", multi_host=True)
            == "postgresql://host/db?sslmode=require"
        )

    def test_safe_url_malformed_multi_host_redacts_every_password(self) -> None:
        """Every userinfo password in a malformed multi-host DSN is redacted."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url(
                "postgresql://u:p@bad host1,u2:p2@bad host2/db", multi_host=True
            )
            == "postgresql://u:***@bad host1,u2:***@bad host2/db"
        )

    def test_safe_url_multi_host_redacts_each(self) -> None:
        """Multi-host Postgres DSNs have each password redacted."""
        from grelmicro._redact import redact_url  # noqa: PLC0415

        assert (
            redact_url("postgresql://u:p@h1,u:p@h2/db", multi_host=True)
            == "postgresql://u:***@h1,u:***@h2/db"
        )

    def test_repr_never_exposes_password(self) -> None:
        """`repr()` uses the redacted URL form."""
        provider = PostgresProvider(URL)
        assert "test_password" not in repr(provider)
        assert "***" in repr(provider)


class TestBuilders:
    """Pure-sugar `.lock()` builders."""

    def test_lock_builder_binds_provider(self) -> None:
        """`provider.lock()` returns an adapter borrowing the provider."""
        provider = PostgresProvider(URL)

        adapter = provider.lock()

        assert isinstance(adapter, PostgresLockAdapter)
        assert adapter.provider is provider
        assert adapter._owns_provider is False

    def test_cache_factory_builds_postgres_adapter(self) -> None:
        """`provider.cache()` builds a `PostgresCacheAdapter`."""
        provider = PostgresProvider(URL)
        adapter = provider.cache()
        assert isinstance(adapter, PostgresCacheAdapter)
        assert adapter.provider is provider

    def test_ratelimiter_factory_builds_postgres_adapter(self) -> None:
        """`provider.ratelimiter()` builds a `PostgresRateLimiterAdapter`."""
        provider = PostgresProvider(URL)
        adapter = provider.ratelimiter()
        assert isinstance(adapter, PostgresRateLimiterAdapter)
        assert adapter.provider is provider

    def test_base_ratelimiter_factory_raises_not_implemented(self) -> None:
        """The base `Provider.ratelimiter` raises for providers that don't override it."""
        provider = PostgresProvider(URL)
        with pytest.raises(
            NotImplementedError, match="no rate limiter adapter"
        ):
            Provider.ratelimiter(provider)

    def test_base_cache_factory_raises_not_implemented(self) -> None:
        """The base `Provider.cache` raises for providers that don't override it."""
        provider = PostgresProvider(URL)
        with pytest.raises(NotImplementedError, match="no cache adapter"):
            Provider.cache(provider)

    def test_circuitbreaker_factory_builds_postgres_adapter(self) -> None:
        """`provider.circuitbreaker()` builds a `PostgresCircuitBreakerAdapter`."""
        provider = PostgresProvider(URL)
        adapter = provider.circuitbreaker()
        assert isinstance(adapter, PostgresCircuitBreakerAdapter)
        assert adapter.provider is provider

    def test_base_circuitbreaker_factory_raises_not_implemented(self) -> None:
        """The base `Provider.circuitbreaker` raises for providers that don't override it."""
        provider = PostgresProvider(URL)
        with pytest.raises(
            NotImplementedError, match="no circuit breaker adapter"
        ):
            Provider.circuitbreaker(provider)


class TestRebindProvider:
    """`_rebind_provider` swaps the bound provider on the adapter."""

    def test_sync_adapter_rebind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PostgresLockAdapter rebinds to a new provider."""
        monkeypatch.setenv("POSTGRES_URL", URL)
        adapter = PostgresLockAdapter()
        assert adapter._owns_provider is True
        owned = PostgresProvider(URL)

        adapter._rebind_provider(owned)

        assert adapter.provider is owned
        assert adapter._owns_provider is False

    def test_ratelimiter_adapter_rebind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PostgresRateLimiterAdapter rebinds to a new provider."""
        monkeypatch.setenv("POSTGRES_URL", URL)
        adapter = PostgresRateLimiterAdapter()
        assert adapter._owns_provider is True
        owned = PostgresProvider(URL)

        adapter._rebind_provider(owned)

        assert adapter.provider is owned
        assert adapter._owns_provider is False

    def test_breaker_adapter_rebind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PostgresCircuitBreakerAdapter rebinds to a new provider."""
        monkeypatch.setenv("POSTGRES_URL", URL)
        adapter = PostgresCircuitBreakerAdapter()
        assert adapter._owns_provider is True
        owned = PostgresProvider(URL)

        adapter._rebind_provider(owned)

        assert adapter.provider is owned
        assert adapter._owns_provider is False


class TestBreakerOwnedLifecycle:
    """An owned provider is opened and closed by the breaker adapter."""

    async def test_owned_provider_opened_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An implicit provider is opened on enter and closed on exit."""
        monkeypatch.setenv("POSTGRES_URL", URL)
        adapter = PostgresCircuitBreakerAdapter()
        assert adapter._owns_provider is True

        pool = make_pool()
        pool.close = AsyncMock()
        adapter.provider._pool = pool

        async with adapter:
            assert adapter.provider.client is pool

        pool.close.assert_awaited_once()


class TestSharingCache:
    """`Grelmicro` dedupes implicit providers by `(class, env_prefix)`."""

    async def test_two_adapters_same_env_prefix_share_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two adapters with the same default env_prefix share one provider."""
        monkeypatch.setenv("POSTGRES_URL", URL)

        first = PostgresLockAdapter()
        second = PostgresLockAdapter(table_name="other_locks")
        assert first.provider is not second.provider

        from grelmicro.coordination._component import (  # noqa: PLC0415
            Coordination,
        )

        pool = make_pool()
        pool.close = AsyncMock()
        for ad in (first, second):
            ad.provider._pool = pool

        micro = Grelmicro(
            uses=[
                Coordination(lock=first),
                Coordination(lock=second, name="other"),
            ]
        )
        async with micro:
            assert first.provider is second.provider
            assert first._owns_provider is True
            assert second._owns_provider is False

    async def test_different_env_prefixes_keep_separate_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct env_prefixes keep distinct providers."""
        monkeypatch.setenv("WRITE_POSTGRES_URL", URL)
        monkeypatch.setenv("READ_POSTGRES_URL", URL)

        write = PostgresLockAdapter(env_prefix="WRITE_POSTGRES_")
        read = PostgresLockAdapter(
            env_prefix="READ_POSTGRES_", table_name="read_locks"
        )

        from grelmicro.coordination._component import (  # noqa: PLC0415
            Coordination,
        )

        for ad in (write, read):
            pool = make_pool()
            pool.close = AsyncMock()
            ad.provider._pool = pool

        micro = Grelmicro(
            uses=[
                Coordination(lock=write),
                Coordination(lock=read, name="read"),
            ]
        )
        async with micro:
            assert write.provider is not read.provider


class TestConfigRedaction:
    """`PostgresConfig` must not expose the credential embedded in the URL."""

    def test_repr_redacts_url_password(self) -> None:
        """`repr()` shows the DSN with the password replaced."""
        config = PostgresConfig(url=URL)

        assert "test_password" not in repr(config)
        assert "***" in repr(config)

    def test_json_dump_redacts_url_password(self) -> None:
        """`model_dump_json()` emits the redacted DSN."""
        config = PostgresConfig(url=URL)

        assert "test_password" not in config.model_dump_json()

    def test_python_dump_does_not_leak(self) -> None:
        """`model_dump()` keeps the wrapper, so printing it stays safe."""
        config = PostgresConfig(url=URL)

        assert "test_password" not in repr(config.model_dump())

    def test_multi_host_url_redacts_every_password(self) -> None:
        """Every host of a multi-host DSN is redacted."""
        config = PostgresConfig(
            url="postgresql://u:test_password@a:5432,b:5432/db"
        )

        assert "test_password" not in repr(config)

    def test_provider_still_receives_the_real_url(self) -> None:
        """The provider built from the config connects with the real DSN."""
        provider = PostgresProvider.from_config(PostgresConfig(url=URL))

        assert provider.url == URL


class TestValidationErrors:
    """A rejected value must never carry its credential into the error."""

    def test_invalid_url_does_not_echo_the_password(self) -> None:
        """A wrong scheme reports the failure without the input."""
        with pytest.raises(ValidationError) as excinfo:
            PostgresConfig(url="redis://usr:test_password@h/0")

        assert "test_password" not in str(excinfo.value)


BAD_URLS = [
    "mysql://test_host:5432/test_db",
    "redis://test_host:6379/0",
    "postgresql://test_host:notaport/test_db",
    "not a url",
]


class TestUrlValidationParity:
    """The constructor, the environment, and the config accept one set."""

    @pytest.mark.parametrize("url", BAD_URLS)
    def test_constructor_refuses_what_the_environment_refuses(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both paths reject the same URL, and with the same error."""
        monkeypatch.setenv("POSTGRES_URL", url)

        with pytest.raises(SettingsValidationError) as from_env:
            PostgresProvider()
        with pytest.raises(SettingsValidationError) as from_kwarg:
            PostgresProvider(url, env_load=False)

        assert str(from_kwarg.value) == str(from_env.value)

    @pytest.mark.parametrize("url", BAD_URLS)
    def test_the_config_refuses_it_too(self, url: str) -> None:
        """`PostgresConfig` rejects every URL the other two paths reject."""
        with pytest.raises(ValidationError):
            PostgresConfig(url=url)

    def test_a_rejected_constructor_url_does_not_echo_the_password(
        self,
    ) -> None:
        """A wrong scheme reports the failure without the credential."""
        with pytest.raises(SettingsValidationError) as excinfo:
            PostgresProvider("mysql://usr:test_password@h/db", env_load=False)

        assert "test_password" not in str(excinfo.value)


DRIVER_SCHEMES = [
    "postgresql+asyncpg",
    "postgresql+pg8000",
    "postgresql+psycopg",
    "postgresql+psycopg2",
    "postgresql+psycopg2cffi",
    "postgresql+py-postgresql",
    "postgresql+pygresql",
]


class TestSQLAlchemyDsn:
    """A SQLAlchemy-style DSN loses its driver suffix, never the rest."""

    @pytest.mark.parametrize("scheme", DRIVER_SCHEMES)
    def test_driver_suffix_is_dropped(self, scheme: str) -> None:
        """Every driver-qualified scheme resolves to plain `postgresql`."""
        # Arrange
        url = f"{scheme}://test_user:test_password@test_host:1234/test_db"
        # Act
        provider = PostgresProvider(url=url)
        # Assert
        assert provider.url == URL

    def test_plain_scheme_is_untouched(self) -> None:
        """A URL with no driver suffix passes through unchanged."""
        # Arrange / Act
        provider = PostgresProvider(url=URL)
        # Assert
        assert provider.url == URL

    def test_postgres_scheme_is_untouched(self) -> None:
        """The short `postgres://` scheme keeps its own spelling."""
        # Arrange
        url = "postgres://test_user:test_password@test_host:1234/test_db"
        # Act
        provider = PostgresProvider(url=url)
        # Assert
        assert provider.url == url

    def test_driver_suffix_is_dropped_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env path normalises the scheme the same way."""
        # Arrange
        monkeypatch.setenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://test_user:test_password@test_host:1234/test_db",
        )
        # Act
        provider = PostgresProvider()
        # Assert
        assert provider.url == URL

    def test_driver_suffix_is_dropped_from_a_config(self) -> None:
        """`from_config` normalises the scheme the same way."""
        # Arrange
        config = PostgresConfig(
            url="postgresql+asyncpg://test_user:test_password@test_host:1234/test_db"
        )
        # Act
        provider = PostgresProvider.from_config(config)
        # Assert
        assert provider.url == URL

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://test_user:pa+ss@test_host:1234/test_db",
            "postgresql://test_user:test_password@test_host:1234/test+db",
        ],
    )
    def test_a_plus_outside_the_scheme_is_kept(self, url: str) -> None:
        """A `+` in a credential or a database name is not a driver suffix."""
        # Arrange / Act
        provider = PostgresProvider(url=url)
        # Assert
        assert provider.url == url

    @pytest.mark.parametrize(
        "scheme", ["POSTGRESQL+ASYNCPG", "PostgreSQL+AsyncPG"]
    )
    def test_an_uppercase_driver_scheme_is_dropped(self, scheme: str) -> None:
        """A URL scheme is case-insensitive, so the suffix still goes."""
        # Arrange
        url = f"{scheme}://test_user:test_password@test_host:1234/test_db"
        # Act
        provider = PostgresProvider(url=url)
        # Assert
        assert provider.url == URL

    def test_multi_host_url_keeps_every_host(self) -> None:
        """Normalising the scheme leaves a multi-host URL intact."""
        # Arrange
        url = "postgresql+asyncpg://u:pw@h1:5432,h2:5433/db"
        # Act
        provider = PostgresProvider(url=url)
        # Assert
        assert provider.url == "postgresql://u:pw@h1:5432,h2:5433/db"

    def test_password_is_still_redacted(self) -> None:
        """Normalising the scheme does not leak the credential."""
        # Arrange
        url = "postgresql+asyncpg://test_user:test_password@test_host:1234/test_db"
        # Act
        provider = PostgresProvider(url=url)
        # Assert
        assert "test_password" not in provider.safe_url
        assert provider.safe_url.startswith("postgresql://")


@requires_sqlalchemy
@pytest.mark.integration
@pytest.mark.timeout(120)
class TestFromEngineAgainstPostgres:
    """`from_engine` against a real server, where the sharing has to hold."""

    @pytest.fixture
    async def engine(self) -> AsyncGenerator["AsyncEngine"]:
        """Provide an app-owned async engine pointing at a container."""
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

        with PostgresContainer() as container:
            port = container.get_exposed_port(5432)
            engine = create_async_engine(
                f"postgresql+asyncpg://test:test@localhost:{port}/test",
                pool_size=5,
            )
            try:
                yield engine
            finally:
                await engine.dispose()

    async def test_lock_round_trip(self, engine: "AsyncEngine") -> None:
        """A lock acquires and releases through the borrowed engine."""
        provider = PostgresProvider.from_engine(engine)

        async with provider, PostgresLockAdapter(provider=provider) as backend:
            name = "engine-lock-" + uuid4().hex
            token = uuid4().hex

            assert await backend.acquire(name=name, token=token, duration=30)
            assert await backend.owned(name=name, token=token)
            assert await backend.release(name=name, token=token)
            assert not await backend.locked(name=name)

    async def test_opens_no_second_pool(self, engine: "AsyncEngine") -> None:
        """Every statement runs on the engine, so the server sees one pool."""
        provider = PostgresProvider.from_engine(engine)

        async with provider:
            await provider.check()
            before = await provider.client.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = 'test'"
            )
            for _ in range(4):
                await provider.check()
            after = await provider.client.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = 'test'"
            )

        assert after <= before + 1

    async def test_borrowed_engine_survives_the_app(
        self, engine: "AsyncEngine"
    ) -> None:
        """`own=False` leaves the engine open for the application to use."""
        async with PostgresProvider.from_engine(engine) as provider:
            await provider.check()

        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None
            assert await driver.fetchval("SELECT 1") == 1

    async def test_owned_engine_is_disposed(
        self, engine: "AsyncEngine"
    ) -> None:
        """`own=True` disposes the engine on exit."""
        pool = engine.pool

        async with PostgresProvider.from_engine(engine, own=True) as provider:
            await provider.check()

        assert engine.pool is not pool

    async def test_lock_survives_a_caller_rollback(
        self, engine: "AsyncEngine"
    ) -> None:
        """A lock taken during a caller transaction outlives its rollback.

        The provider takes its own connection rather than the one the
        caller holds, so grelmicro never writes inside a transaction it
        does not control. Riding the caller's connection would turn the
        acquire into a savepoint and give the lock back on rollback.
        """
        from sqlalchemy import text  # noqa: PLC0415

        provider = PostgresProvider.from_engine(engine)
        name = "engine-rollback-" + uuid4().hex
        token = uuid4().hex

        async with provider, PostgresLockAdapter(provider=provider) as backend:
            with contextlib.suppress(RuntimeError):
                async with engine.begin() as connection:
                    await connection.execute(text("SELECT 1"))
                    assert await backend.acquire(
                        name=name, token=token, duration=30
                    )
                    msg = "the caller transaction fails"
                    raise RuntimeError(msg)

            assert await backend.locked(name=name)
            assert await backend.owned(name=name, token=token)


class _FakeSAConnection:
    """Stand in for a SQLAlchemy `AsyncConnection` checkout."""

    def __init__(self, driver: object, *, raw_error: bool = False) -> None:
        self.driver = driver
        self.raw_error = raw_error
        self.closed = False

    async def start(self) -> "_FakeSAConnection":
        """Return the started connection, as SQLAlchemy does."""
        return self

    async def get_raw_connection(self) -> MagicMock:
        """Return the DBAPI wrapper carrying the asyncpg connection."""
        if self.raw_error:
            msg = "connection is gone"
            raise ConnectionError(msg)
        raw = MagicMock()
        raw.driver_connection = self.driver
        return raw

    async def close(self) -> None:
        """Return the connection to the engine's pool."""
        self.closed = True


class _FakeEngine:
    """Stand in for an `AsyncEngine`, handing out fake checkouts."""

    def __init__(self, *, raw_error: bool = False) -> None:
        self.raw_error = raw_error
        self.connections: list[_FakeSAConnection] = []
        self.disposed = False

    def connect(self) -> _FakeSAConnection:
        """Hand out a fresh checkout and remember it."""
        driver = MagicMock()
        driver.execute = AsyncMock(return_value="EXECUTE 1")
        driver.executemany = AsyncMock()
        driver.fetch = AsyncMock(return_value=["row"])
        driver.fetchrow = AsyncMock(return_value="row")
        driver.fetchval = AsyncMock(return_value=1)
        connection = _FakeSAConnection(driver, raw_error=self.raw_error)
        self.connections.append(connection)
        return connection

    async def dispose(self) -> None:
        """Record that the engine was disposed."""
        self.disposed = True


@requires_sqlalchemy
class TestEnginePool:
    """The pool surface `from_engine` serves to the adapters."""

    def make(self, *, raw_error: bool = False) -> tuple[Any, _FakeEngine]:
        """Build a pool over a fake engine."""
        from grelmicro.providers._sqlalchemy import EnginePool  # noqa: PLC0415

        engine = _FakeEngine(raw_error=raw_error)
        return EnginePool(cast("AsyncEngine", engine)), engine

    async def test_acquire_awaited_hands_the_connection_over(self) -> None:
        """The outbox listener awaits a connection and holds it."""
        pool, engine = self.make()

        conn = await pool.acquire()

        assert conn is engine.connections[0].driver
        assert not engine.connections[0].closed

        await pool.release(conn)
        assert engine.connections[0].closed

    async def test_acquire_entered_releases_on_exit(self) -> None:
        """The context manager form gives the connection back."""
        pool, engine = self.make()

        async with pool.acquire() as conn:
            assert conn is engine.connections[0].driver

        assert engine.connections[0].closed

    async def test_exit_without_enter_releases_nothing(self) -> None:
        """A context manager exited without entering holds no connection."""
        pool, engine = self.make()
        acquire = pool.acquire()

        await acquire.__aexit__(None, None, None)

        assert engine.connections == []

    async def test_release_ignores_an_unknown_connection(self) -> None:
        """Releasing something never borrowed is a no-op."""
        pool, engine = self.make()

        await pool.release(MagicMock())

        assert engine.connections == []

    async def test_checkout_returns_the_connection_on_failure(self) -> None:
        """A failed unwrap gives the checkout back instead of leaking it."""
        pool, engine = self.make(raw_error=True)

        with pytest.raises(ConnectionError):
            await pool.checkout()

        assert engine.connections[0].closed

    async def test_statements_run_on_a_borrowed_connection(self) -> None:
        """Every pool-level statement borrows, runs, and releases."""
        pool, engine = self.make()

        statements = 5

        assert await pool.execute("SELECT $1", 1) == "EXECUTE 1"
        assert await pool.fetch("SELECT $1", 1) == ["row"]
        assert await pool.fetchrow("SELECT $1", 1) == "row"
        assert await pool.fetchval("SELECT $1", 1) == 1
        await pool.executemany("SELECT $1", [(1,), (2,)])

        assert len(engine.connections) == statements
        assert all(connection.closed for connection in engine.connections)

    async def test_close_disposes_the_engine(self) -> None:
        """`close` hands everything back and disposes the engine."""
        pool, engine = self.make()
        await pool.acquire()

        await pool.close()

        assert engine.connections[0].closed
        assert engine.disposed

    async def test_release_all_keeps_the_engine(self) -> None:
        """`release_all` hands everything back but leaves the engine open."""
        pool, engine = self.make()
        await pool.acquire()

        await pool.release_all()

        assert engine.connections[0].closed
        assert not engine.disposed

    async def test_borrowed_engine_released_on_provider_exit(self) -> None:
        """A provider that does not own the engine still gives connections back."""
        pool, engine = self.make()
        provider = PostgresProvider.from_client(pool)
        await pool.acquire()

        async with provider:
            pass

        assert engine.connections[0].closed
        assert not engine.disposed


@requires_sqlalchemy
class TestEngineDriver:
    """`from_engine` checks the driver, not just the backend."""

    def test_non_asyncpg_driver_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Postgres engine on another driver cannot serve asyncpg."""
        engine = create_async_engine(ASYNCPG_URL)
        monkeypatch.setattr(engine.sync_engine.dialect, "driver", "psycopg")

        with pytest.raises(SettingsValidationError, match="driver should be"):
            PostgresProvider.from_engine(engine)
