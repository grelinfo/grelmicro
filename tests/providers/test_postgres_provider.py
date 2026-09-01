"""Tests for `PostgresProvider`."""

import asyncio
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

        redacted = "postgresql://test_user:***@test_host:1234/test_db"
        assert provider.url == redacted
        assert provider.safe_url == redacted
        assert "test_password" not in repr(provider)

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

    async def test_session_state_survives_a_loan(
        self, engine: "AsyncEngine"
    ) -> None:
        """The application's own session state outlives grelmicro's use.

        SQLAlchemy applies session state once per physical connection, in
        a `connect` event, and never re-applies it on checkout. A full
        `RESET ALL` on release would clear it and leave the application
        querying the wrong schema.
        """
        from sqlalchemy import event  # noqa: PLC0415

        @event.listens_for(engine.sync_engine, "connect")
        def _set_search_path(dbapi_connection: Any, _record: object) -> None:  # noqa: ANN401
            dbapi_connection.await_(
                dbapi_connection.driver_connection.execute(
                    "SET search_path TO grelmicro_probe, public"
                )
            )

        provider = PostgresProvider.from_engine(engine)

        async with provider:
            await provider.check()

        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None
            search_path = await driver.fetchval("SHOW search_path")

        assert "grelmicro_probe" in search_path

    async def test_outbox_round_trip(self, engine: "AsyncEngine") -> None:
        """A jsonb payload survives the engine's own type codecs.

        The engine registers a `jsonb` codec, so a borrowed connection
        hands back a decoded mapping where a plain asyncpg pool hands back
        a string. The outbox has to read both.
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

        from grelmicro.outbox._message import OutboxRecord  # noqa: PLC0415
        from grelmicro.outbox.postgres import (  # noqa: PLC0415
            PostgresOutboxAdapter,
        )

        provider = PostgresProvider.from_engine(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        topic = "orders-" + uuid4().hex
        payload = {"amount": 42, "nested": {"x": [1, 2]}}

        async with provider, PostgresOutboxAdapter(provider=provider) as outbox:
            record = OutboxRecord(
                id=uuid4(),
                topic=topic,
                key="k1",
                payload=payload,
                headers={"trace": "abc"},
            )
            async with session_factory() as session, session.begin():
                assert await outbox.enqueue(session, record)

            claimed = await outbox.claim(topics=[topic], limit=10, lease=30)

        assert len(claimed) == 1
        assert claimed[0].payload == payload
        assert claimed[0].headers == {"trace": "abc"}

    async def test_leader_election_metadata_round_trip(
        self, engine: "AsyncEngine"
    ) -> None:
        """The other jsonb writer reads back what it wrote over an engine."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresLeaderElectionAdapter,
        )

        provider = PostgresProvider.from_engine(engine)
        metadata = {"region": "eu", "pod": "a-1"}

        async with (
            provider,
            PostgresLeaderElectionAdapter(provider=provider) as backend,
        ):
            record = await backend.acquire_or_renew(
                name="svc-" + uuid4().hex,
                token=uuid4().hex,
                duration=30,
                metadata=metadata,
            )

        assert record.metadata == metadata

    async def test_a_static_pool_engine_is_refused(
        self, engine: "AsyncEngine"
    ) -> None:
        """`StaticPool` shares one connection, so two checkouts collide."""
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        static = create_async_engine(
            engine.url.render_as_string(hide_password=False),
            poolclass=StaticPool,
        )
        provider = PostgresProvider.from_engine(static)

        try:
            async with provider:
                await provider.check()
                async with provider.client.acquire():
                    with pytest.raises(SettingsValidationError, match="two"):
                        await provider.client.acquire().__aenter__()
        finally:
            await static.dispose()

    async def test_reentry_reuses_the_engine(
        self, engine: "AsyncEngine"
    ) -> None:
        """A provider entered twice never opens a private pool."""
        from grelmicro.providers._sqlalchemy import EnginePool  # noqa: PLC0415

        for own in (False, True):
            provider = PostgresProvider.from_engine(engine, own=own)

            async with provider:
                await provider.check()
            # The borrowed default is the form that broke: its facade is
            # closed on exit, so a second enter has to build a fresh one
            # rather than hand back the closed one.
            async with provider:
                await provider.check()
                assert isinstance(provider.client, EnginePool)

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
        self.invalidated = False

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

    async def invalidate(self) -> None:
        """Mark the connection as never to be pooled again."""
        self.invalidated = True

    async def close(self) -> None:
        """Return the connection to the engine's pool."""
        self.closed = True


class _FakeEngine:
    """Stand in for an `AsyncEngine`, handing out fake checkouts."""

    def __init__(
        self, *, raw_error: bool = False, reset_error: bool = False
    ) -> None:
        self.raw_error = raw_error
        self.reset_error = reset_error
        self.alias: Any = None
        self.channels: set[str] = set()
        self.connections: list[_FakeSAConnection] = []
        self.disposed = False
        self.closing: Any = None

    def connect(self) -> _FakeSAConnection:
        """Hand out a fresh checkout and remember it.

        When `closing` is set, the pool is closed part way through, which
        is the race a real `close()` runs against an opening checkout.
        """
        if self.closing is not None:
            self.closing._closed = True
        driver = MagicMock()
        driver.execute = AsyncMock(return_value="EXECUTE 1")
        driver.executemany = AsyncMock()
        driver.fetch = AsyncMock(return_value=["row"])
        driver.fetchrow = AsyncMock(return_value="row")
        driver.fetchval = AsyncMock(return_value=1)
        driver.is_in_transaction = MagicMock(return_value=False)
        driver._top_xact = None
        driver._listeners = dict.fromkeys(self.channels, object())
        driver._log_listeners = set()
        if self.reset_error:
            driver.execute = AsyncMock(
                side_effect=ConnectionError("clean failed")
            )
        if self.alias is not None:
            driver = self.alias
        connection = _FakeSAConnection(driver, raw_error=self.raw_error)
        self.connections.append(connection)
        return connection

    async def dispose(self) -> None:
        """Record that the engine was disposed."""
        self.disposed = True


@requires_sqlalchemy
class TestEnginePool:
    """The pool surface `from_engine` serves to the adapters."""

    def make(
        self, *, raw_error: bool = False, reset_error: bool = False
    ) -> tuple[Any, _FakeEngine]:
        """Build a pool over a fake engine."""
        from grelmicro.providers._sqlalchemy import EnginePool  # noqa: PLC0415

        engine = _FakeEngine(raw_error=raw_error, reset_error=reset_error)
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

    async def test_release_cleans_the_connection(self) -> None:
        """A connection is cleaned before it goes back to the application."""
        pool, engine = self.make()

        conn = await pool.acquire()
        conn._listeners["grelmicro_outbox"] = object()
        await pool.release(conn)

        conn.execute.assert_awaited_with('UNLISTEN "grelmicro_outbox"')
        assert engine.connections[0].closed
        assert not engine.connections[0].invalidated

    async def test_unresettable_connection_is_not_pooled(self) -> None:
        """A connection that cannot be reset is dropped, never handed back."""
        pool, engine = self.make(reset_error=True)

        conn = await pool.acquire()
        conn.is_in_transaction = MagicMock(return_value=True)
        await pool.release(conn)

        assert engine.connections[0].invalidated

    async def test_checkout_after_close_is_refused(self) -> None:
        """A closed pool refuses to open a fresh connection."""
        from asyncpg import InterfaceError  # noqa: PLC0415

        pool, _ = self.make()
        await pool.close()

        with pytest.raises(InterfaceError, match="pool is closed"):
            await pool.checkout()

    async def test_release_all_survives_one_failure(self) -> None:
        """One connection that will not reset never strands the others."""
        pool, engine = self.make(reset_error=True)
        borrowed = 2

        for _ in range(borrowed):
            await pool.acquire()

        await pool.release_all()

        assert len(engine.connections) == borrowed
        assert all(c.invalidated for c in engine.connections)

    async def test_a_driver_without_the_records_still_returns_it(self) -> None:
        """An asyncpg that renamed its private records never burns the pool.

        The records are private to the driver. Reaching for one that has
        gone must leave the connection returnable, because the server-side
        cleanup still runs.
        """
        pool, engine = self.make()

        from grelmicro.providers import _sqlalchemy  # noqa: PLC0415

        _sqlalchemy._reported_driver_changes.clear()

        conn = await pool.acquire()
        del conn._top_xact
        del conn._listeners

        await pool.release(conn)

        assert engine.connections[0].closed
        assert not engine.connections[0].invalidated

        # Said once per record, not once per release: an app taking one
        # lock per request would otherwise warn on every request.
        reported = set(_sqlalchemy._reported_driver_changes)
        second = await pool.acquire()
        del second._top_xact
        await pool.release(second)

        assert set(_sqlalchemy._reported_driver_changes) == reported

    async def test_clean_clears_a_stale_transaction_marker(self) -> None:
        """A cancel between the marker and `BEGIN` leaves it set.

        asyncpg records the transaction before it sends `BEGIN`, so the
        marker can outlive a cancelled start with no transaction open.
        The next `transaction()` would then read as a nested one.
        """
        pool, _ = self.make()

        conn = await pool.acquire()
        conn._top_xact = object()
        await pool.release(conn)

        assert conn._top_xact is None
        conn.execute.assert_awaited_with("ROLLBACK")

    async def test_held_clean_forgets_listeners_on_the_client(self) -> None:
        """`UNLISTEN` on the server has to be matched on the client.

        `add_listener` skips the round trip for a channel it believes is
        subscribed, so a registry left behind makes a later `LISTEN`
        silently do nothing.
        """
        pool, engine = self.make()
        engine.channels = {"app_channel"}

        conn = await pool.acquire()
        conn._listeners["outbox"] = object()
        await pool.release(conn)

        # The app's own subscription survives the loan, grelmicro's does not.
        assert "app_channel" in conn._listeners
        assert "outbox" not in conn._listeners

    async def test_checkout_racing_close_is_refused(self) -> None:
        """A checkout that lands after `close()` drained is never stranded."""
        from asyncpg import InterfaceError  # noqa: PLC0415

        pool, engine = self.make()
        engine.closing = pool

        with pytest.raises(InterfaceError, match="pool is closed"):
            await pool.checkout()

        assert not pool._borrowed
        assert engine.connections[0].closed

    async def test_block_release_sends_nothing(self) -> None:
        """A connection borrowed for a block leaves nothing to undo.

        The statement helpers and every `async with acquire()` run on
        this path, so the clean must cost no round trip.
        """
        pool, engine = self.make()

        async with pool.acquire() as conn:
            pass

        conn.execute.assert_not_awaited()
        assert engine.connections[0].closed

    async def test_helper_release_sends_nothing(self) -> None:
        """A pool-level statement pays for itself and nothing more."""
        pool, engine = self.make()

        await pool.fetchval("SELECT 1")

        driver = cast("Any", engine.connections[0].driver)
        driver.execute.assert_not_awaited()

    async def test_block_release_rolls_back_an_open_transaction(self) -> None:
        """A block that left a transaction open is still rolled back."""
        pool, engine = self.make()

        async with pool.acquire() as conn:
            conn.is_in_transaction = MagicMock(return_value=True)
            conn._top_xact = object()

        conn.execute.assert_awaited_once_with("ROLLBACK")
        # The marker has to be cleared in this case above all: an `or` that
        # short-circuits past the clear leaves it set on a connection the
        # application pools next.
        assert conn._top_xact is None
        assert not engine.connections[0].invalidated

    async def test_clean_that_never_answers_is_dropped(self) -> None:
        """A server that stops answering never holds the release open."""
        pool, engine = self.make()

        conn = await pool.acquire()
        conn.is_in_transaction = MagicMock(return_value=True)
        conn.execute = AsyncMock(side_effect=asyncio.TimeoutError)

        await pool.release(conn)

        assert engine.connections[0].invalidated

    async def test_a_refused_close_is_reported_not_raised(self) -> None:
        """A failed check-in never replaces the caller's own error."""
        pool, engine = self.make()

        conn = await pool.acquire()
        engine.connections[0].close = AsyncMock(  # type: ignore[method-assign]
            side_effect=ConnectionError("gone")
        )

        await pool.release(conn)

    async def test_held_clean_rolls_back_an_open_transaction(self) -> None:
        """A held connection rolls back and drops the channels it added."""
        pool, _ = self.make()

        conn = await pool.acquire()
        conn.is_in_transaction = MagicMock(return_value=True)
        conn._listeners["outbox"] = object()
        await pool.release(conn)

        conn.execute.assert_awaited_with('ROLLBACK; UNLISTEN "outbox"')

    async def test_a_cancelled_caller_still_sees_the_cancel(self) -> None:
        """The cleanup finishes, and the caller's own cancel survives it."""
        pool, engine = self.make()

        conn = await pool.acquire()

        async def slow_clean(*_: object, **__: object) -> None:
            await asyncio.sleep(0.05)

        conn.execute = AsyncMock(side_effect=slow_clean)
        task = asyncio.create_task(pool.release(conn))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert engine.connections[0].closed
        assert not engine.connections[0].invalidated

    async def test_cancelled_clean_drops_the_connection(self) -> None:
        """A cleanup cancelled by the connection never cancels the caller.

        The cancel says the connection went away, not that the caller
        asked to stop, so reporting it upwards would cancel the siblings
        of a task nobody interrupted.
        """
        pool, engine = self.make()

        conn = await pool.acquire()
        conn.is_in_transaction = MagicMock(return_value=True)
        conn.execute = AsyncMock(side_effect=asyncio.CancelledError)

        await pool.release(conn)

        assert engine.connections[0].invalidated

    async def test_release_all_survives_a_failing_discard(self) -> None:
        """A connection that cannot even be dropped never stalls shutdown."""
        pool, engine = self.make(reset_error=True)

        await pool.acquire()
        await pool.acquire()
        for connection in engine.connections:
            connection.invalidate = AsyncMock(  # type: ignore[method-assign]
                side_effect=ConnectionError("gone")
            )

        await pool.release_all()

        assert all(connection.closed for connection in engine.connections)

    async def test_release_all_survives_a_raising_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A release that refuses never strands the connections behind it."""
        pool, _ = self.make()
        borrowed = 2

        for _ in range(borrowed):
            await pool.acquire()

        calls = 0
        real = type(pool).release

        async def refuse_once(
            this: Any,  # noqa: ANN401
            conn: object,
            *,
            discard: bool = False,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                this._borrowed.pop(conn, None)
                msg = "check-in refused"
                raise ConnectionError(msg)
            await real(this, conn, discard=discard)

        monkeypatch.setattr(type(pool), "release", refuse_once)

        await pool.release_all()

        assert calls == borrowed
        assert not pool._borrowed

    async def test_release_all_survives_a_cancelled_release(self) -> None:
        """A shutdown cancelled part way still hands back the rest."""
        pool, engine = self.make()
        borrowed = 2

        for _ in range(borrowed):
            conn = await pool.acquire()
            conn.execute = AsyncMock(side_effect=asyncio.CancelledError)

        await pool.release_all()

        assert len(engine.connections) == borrowed
        assert all(c.invalidated for c in engine.connections)

    async def test_an_aliasing_pool_is_refused(self) -> None:
        """A pool that hands one connection to two checkouts cannot be shared.

        `StaticPool` does exactly this. Whichever checkout released first
        would clean and return the other one's connection, and the other
        would find nothing left to give back.
        """
        pool, engine = self.make()

        first = await pool.acquire()
        engine.alias = first

        with pytest.raises(SettingsValidationError, match="two"):
            await pool.acquire()

        assert engine.connections[1].closed
        assert pool._borrowed

    async def test_shutdown_stops_serving_a_borrowed_engine(self) -> None:
        """A borrowed engine stops answering once the app has shut down.

        Nothing hands a connection back after the drain, so a task that
        outlived the app must not keep taking them from the application's
        engine.
        """
        from asyncpg import InterfaceError  # noqa: PLC0415

        pool, engine = self.make()

        await pool.shutdown(dispose=False)

        assert not engine.disposed
        with pytest.raises(InterfaceError, match="pool is closed"):
            await pool.checkout()

    async def test_close_waits_for_a_release_in_flight(self) -> None:
        """A release already running is finished before the engine goes.

        `release` pops its connection out of the record before it awaits,
        so `release_all` cannot see it. Disposing the engine underneath
        would fail its cleanup and cost the application a connection.
        """
        pool, engine = self.make()

        conn = await pool.acquire()

        async def slow_clean(*_: object, **__: object) -> None:
            await asyncio.sleep(0.05)

        conn.execute = AsyncMock(side_effect=slow_clean)
        releasing = asyncio.create_task(pool.release(conn))
        await asyncio.sleep(0)

        await pool.close()

        assert releasing.done()
        assert engine.connections[0].closed
        assert engine.disposed

    async def test_close_disposes_the_engine(self) -> None:
        """`close` hands everything back and disposes the engine."""
        pool, engine = self.make()
        await pool.acquire()

        await pool.close()

        assert engine.connections[0].closed
        assert engine.disposed

    async def test_release_all_drops_a_leaked_checkout(self) -> None:
        """A connection nobody gave back is dropped, never pooled.

        Its holder may still be running statements on it, and handing it
        to the application would put two coroutines on one connection.
        """
        pool, engine = self.make()
        await pool.acquire()

        await pool.release_all()

        assert engine.connections[0].closed
        assert engine.connections[0].invalidated
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
