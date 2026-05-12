"""Postgres Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from asyncpg import Pool, create_pool
from pydantic import BaseModel, PostgresDsn, ValidationError
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Doc

from grelmicro.errors import OutOfContextError, SettingsValidationError

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.sync.postgres import PostgresSyncAdapter


class PostgresConfig(BaseModel):
    """Postgres connection settings.

    Plain `BaseModel` (env-free). Pass to `PostgresProvider.from_config(cfg)`
    or build a `PostgresProvider` directly from kwargs. The env path lives
    on the provider, not the config.
    """

    url: PostgresDsn | None = None
    host: str | None = None
    port: int = 5432
    database: str | None = None
    user: str | None = None
    password: str | None = None


class _PostgresEnvSettings(BaseSettings):
    """Read Postgres settings from the environment (env_prefix-driven).

    The `db` field maps to `{env_prefix}DB`, matching the Postgres
    convention (the `postgres` Docker image, libpq, ...). It is
    surfaced as `database` everywhere else in the public API.
    """

    model_config = SettingsConfigDict(extra="ignore")

    url: PostgresDsn | None = None
    host: str | None = None
    port: int = 5432
    db: str | None = None
    user: str | None = None
    password: str | None = None


class PostgresProvider:
    """Postgres connection provider.

    Holds the resolved URL and an asyncpg connection pool. Adapters
    (`PostgresSyncAdapter`, ...) borrow the pool from a provider
    instead of opening their own, so multiple components against the
    same Postgres share one pool.

    Construction forms (FastStream-style):

    ```python
    PostgresProvider("postgresql://localhost:5432/app")  # positional URL
    PostgresProvider(url="postgresql://...")             # keyword URL
    PostgresProvider(                                    # decomposed kwargs
        host="db", port=5432, database="app",
        user="u", password="pw",
    )
    PostgresProvider()                                   # env-driven (POSTGRES_*)
    PostgresProvider(env_prefix="WRITE_POSTGRES_")       # custom env prefix
    PostgresProvider.from_config(PostgresConfig(...))    # from a config object
    PostgresProvider.from_client(pool)                   # bring-your-own pool
    ```

    The provider is an async context manager: enter it to open the
    asyncpg pool, exit to close it. Adapters delegate their lifecycle
    to the provider when one is supplied.

    Read more in the [Providers](../providers.md) docs.
    """

    short_name: ClassVar[str] = "postgres"

    def __init__(
        self,
        url: Annotated[
            PostgresDsn | str | None,
            Doc(
                """
                The Postgres URL. Mutually exclusive with `host`.
                """,
            ),
        ] = None,
        *,
        host: Annotated[
            str | None,
            Doc("Postgres host. Mutually exclusive with `url`."),
        ] = None,
        port: Annotated[int | None, Doc("Postgres port.")] = None,
        database: Annotated[str | None, Doc("Postgres database name.")] = None,
        user: Annotated[str | None, Doc("Postgres user.")] = None,
        password: Annotated[str | None, Doc("Postgres password.")] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix. Defaults to `POSTGRES_` so
                `POSTGRES_URL`, `POSTGRES_HOST`, ... are read out of the box.
                Override to split pools: `WRITE_POSTGRES_`, `READ_POSTGRES_`.
                """,
            ),
        ] = "POSTGRES_",
        env_load: Annotated[
            bool,
            Doc(
                """
                When True (default), missing kwargs fall back to
                environment variables under `env_prefix`. Set to False
                to use kwargs only and never touch the environment.
                """,
            ),
        ] = True,
    ) -> None:
        """Initialize the provider and resolve the connection URL."""
        self._env_prefix = env_prefix
        self._url = _resolve_url(
            url=url,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        self._pool: Pool | None = None
        self._own = True

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            PostgresConfig,
            Doc("Pre-built `PostgresConfig` carrying the connection settings."),
        ],
        *,
        env_prefix: str = "POSTGRES_",
    ) -> Self:
        """Build a provider from a `PostgresConfig` instance.

        The config is treated as authoritative: no environment reads.
        """
        return cls(
            url=config.url.unicode_string() if config.url else None,
            host=config.host,
            port=config.port,
            database=config.database,
            user=config.user,
            password=config.password,
            env_prefix=env_prefix,
            env_load=False,
        )

    @classmethod
    def from_client(
        cls,
        client: Annotated[
            Pool,
            Doc("A pre-built `asyncpg.Pool`."),
        ],
        *,
        own: Annotated[
            bool,
            Doc(
                """
                When True, the provider closes the pool on `__aexit__`.
                When False (default), the caller keeps ownership and
                must close the pool themselves.
                """,
            ),
        ] = False,
    ) -> Self:
        """Build a provider that wraps an existing asyncpg pool.

        Use this when you already own a Postgres pool (custom retry,
        ssl context, testcontainers fixture, ...) and want grelmicro
        components to use it.
        """
        self = cls.__new__(cls)
        self._env_prefix = "POSTGRES_"
        self._url = ""
        self._pool = client
        self._own = own
        return self

    @property
    def url(self) -> str:
        """Resolved Postgres URL (empty for `from_client` providers)."""
        return self._url

    @property
    def env_prefix(self) -> str:
        """Environment variable prefix used to resolve missing kwargs."""
        return self._env_prefix

    @property
    def client(self) -> Pool:
        """The underlying `asyncpg.Pool`.

        Raises:
            OutOfContextError: When accessed before `__aenter__`.
        """
        if self._pool is None:
            raise OutOfContextError(self, "client")
        return self._pool

    def sync(self, **kwargs: Any) -> PostgresSyncAdapter:  # noqa: ANN401
        """Build a `PostgresSyncAdapter` bound to this provider."""
        from grelmicro.sync.postgres import PostgresSyncAdapter  # noqa: PLC0415

        return PostgresSyncAdapter(provider=self, **kwargs)

    async def __aenter__(self) -> Self:
        """Open the asyncpg pool when the provider owns it."""
        if self._pool is None:
            self._pool = await create_pool(self._url)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the pool when the provider owns it."""
        if self._own and self._pool is not None:
            await self._pool.close()
            self._pool = None


def _resolve_url(
    *,
    url: PostgresDsn | str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    user: str | None,
    password: str | None,
    env_prefix: str,
    env_load: bool,
) -> str:
    """Resolve the connection URL from kwargs and (optionally) the environment."""
    if url is not None and host is not None:
        msg = "pass either `url` or `host`, not both"
        raise PostgresProviderConfigError(msg)

    if url is not None:
        return str(url)

    if host is not None:
        return _compose_url(
            host=host,
            port=port or 5432,
            database=database,
            user=user,
            password=password,
        )

    if not env_load:
        msg = "no `url` or `host` provided and env_load is False"
        raise PostgresProviderConfigError(msg)

    try:
        settings = _PostgresEnvSettings(_env_prefix=env_prefix)  # type: ignore[call-arg]  # ty: ignore[unknown-argument]
    except ValidationError as error:
        raise PostgresProviderConfigError(error) from None

    if settings.url is not None and settings.host is not None:
        msg = f"set either {env_prefix}URL or {env_prefix}HOST, not both"
        raise PostgresProviderConfigError(msg)
    if settings.url is not None:
        return settings.url.unicode_string()
    if settings.host is not None:
        return _compose_url(
            host=settings.host,
            port=settings.port,
            database=settings.db,
            user=settings.user,
            password=settings.password,
        )
    msg = f"either {env_prefix}URL or {env_prefix}HOST must be set"
    raise PostgresProviderConfigError(msg)


def _compose_url(
    *,
    host: str,
    port: int,
    database: str | None,
    user: str | None,
    password: str | None,
) -> str:
    """Compose a `postgresql://` URL from decomposed parts."""
    return MultiHostUrl.build(
        scheme="postgresql",
        username=user,
        password=password,
        host=host,
        port=port,
        path=database,
    ).unicode_string()


class PostgresProviderConfigError(SettingsValidationError):
    """Raised when the Postgres provider cannot resolve a connection URL."""
