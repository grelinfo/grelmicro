"""Postgres Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self, cast

from asyncpg import Pool, create_pool
from pydantic import (
    BaseModel,
    ConfigDict,
    PostgresDsn,
    SecretStr,
    ValidationError,
)
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Doc

from grelmicro._redact import redact_url
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers._base import Provider
from grelmicro.providers._sqlalchemy import EnginePool, validate_engine
from grelmicro.providers._url import validate_url
from grelmicro.types import SecretUrl

if TYPE_CHECKING:
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncEngine

    from grelmicro.cache.postgres import PostgresCacheAdapter
    from grelmicro.coordination.postgres import (
        PostgresLeaderElectionAdapter,
        PostgresLockAdapter,
        PostgresReadWriteLockAdapter,
        PostgresScheduleAdapter,
    )
    from grelmicro.outbox.postgres import PostgresOutboxAdapter
    from grelmicro.resilience.circuitbreaker.postgres import (
        PostgresCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.postgres import (
        PostgresRateLimiterAdapter,
    )


class PostgresConfig(BaseModel):
    """Postgres connection settings.

    Plain `BaseModel` (env-free). Pass to `PostgresProvider.from_config(cfg)`
    or build a `PostgresProvider` directly from kwargs. The env path lives
    on the provider, not the config.

    A rejected value is never echoed back: a mistyped URL would carry the
    password into the `ValidationError` text.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    url: SecretUrl[PostgresDsn] | None = None
    host: str | None = None
    port: int = 5432
    database: str | None = None
    user: str | None = None
    password: SecretStr | None = None
    command_timeout: float | None = None


class _PostgresEnvSettings(BaseSettings):
    """Read Postgres settings from the environment (env_prefix-driven).

    The database name reads from `{env_prefix}DB` (the `postgres` Docker
    image and libpq convention) or `{env_prefix}DATABASE` (the field name
    used elsewhere in the public API). `DB` wins when both are set.
    """

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    url: SecretUrl[PostgresDsn] | None = None
    host: str | None = None
    port: int = 5432
    db: str | None = None
    database: str | None = None
    user: str | None = None
    password: SecretStr | None = None


class _PostgresTimeoutEnvSettings(BaseSettings):
    """Read only the command timeout from the environment.

    Kept separate from `_PostgresEnvSettings` so resolving the timeout never
    validates the connection fields. An explicit-URL provider must not fail
    because an unrelated `{env_prefix}URL` is set in the environment.
    """

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    command_timeout: float | None = None


class PostgresProvider(Provider):
    """Postgres connection provider.

    Holds the resolved URL and an asyncpg connection pool. Adapters
    (`PostgresLockAdapter`, ...) borrow the pool from a provider
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
    PostgresProvider.from_engine(engine)                 # share a SQLAlchemy engine
    ```

    The provider is an async context manager: enter it to open the
    asyncpg pool, exit to close it. Adapters delegate their lifecycle
    to the provider when one is supplied.

    Read more in the [Providers](../providers/index.md) docs.
    """

    short_name: ClassVar[str] = "postgres"
    _asyncpg_instrumented: ClassVar[bool] = False

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
        command_timeout: Annotated[
            float | None,
            Doc(
                """
                Per-operation timeout in seconds for pooled connections. A
                query that runs longer, or that hangs on an unresponsive
                server, raises `TimeoutError` instead of blocking until the
                OS TCP timeout. Defaults to None (no timeout). Reads
                `{env_prefix}COMMAND_TIMEOUT` when unset and `env_load` is
                True.
                """,
            ),
        ] = None,
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
        self._command_timeout = _resolve_command_timeout(
            command_timeout, env_prefix=env_prefix, env_load=env_load
        )
        self._pool: Pool | None = None
        self._engine: AsyncEngine | None = None
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
            url=(
                config.url.get_secret_value().unicode_string()
                if config.url
                else None
            ),
            host=config.host,
            port=config.port,
            database=config.database,
            user=config.user,
            password=(
                config.password.get_secret_value() if config.password else None
            ),
            command_timeout=config.command_timeout,
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
        self._command_timeout = None
        self._pool = client
        self._engine = None
        self._own = own
        return self

    @classmethod
    def from_engine(
        cls,
        engine: Annotated[
            AsyncEngine,
            Doc(
                "A SQLAlchemy `AsyncEngine` on the `postgresql+asyncpg` dialect."
            ),
        ],
        *,
        own: Annotated[
            bool,
            Doc(
                """
                When True, the provider disposes the engine on `__aexit__`.
                When False (default), the caller keeps ownership and
                must dispose the engine themselves.
                """,
            ),
        ] = False,
    ) -> Self:
        """Build a provider that borrows a SQLAlchemy engine's pool.

        Every operation takes a connection from the engine, unwraps it to
        the asyncpg connection underneath, and gives it back, so the
        database sees one pool instead of two.

        The engine must use the `postgresql+asyncpg` dialect. Pass an
        `AsyncEngine`, never an `AsyncSession` or an `AsyncConnection`:
        those carry a transaction the caller has open, and a statement
        grelmicro runs inside it disappears when that transaction rolls
        back.

        `client` then serves the part of the `asyncpg.Pool` surface the
        adapters use: `acquire`, `release`, `execute`, `executemany`,
        `fetch`, `fetchrow`, `fetchval`, and `close`. The pool-management
        calls (`terminate`, `get_size`, `expire_connections`,
        `copy_records_to_table`, and `acquire(timeout=...)`) belong to a
        pool grelmicro opened, so they are absent here. Reach for the
        engine for anything beyond running a statement.

        Raises:
            SettingsValidationError: When the argument is not an
                `AsyncEngine`, or its dialect is not `postgresql+asyncpg`.
        """
        validated = validate_engine(engine)
        self = cls.__new__(cls)
        self._env_prefix = "POSTGRES_"
        # The password stays with the engine. This provider never opens a
        # connection of its own here, so holding the application's
        # credential would add a place for it to leak from and nothing else.
        self._url = _normalize_scheme(validated.url.render_as_string())
        self._command_timeout = None
        self._pool = cast("Pool", EnginePool(validated))
        self._engine = validated
        self._own = own
        return self

    @property
    def url(self) -> str:
        """Resolved Postgres URL (empty for `from_client` providers).

        A `from_engine` provider reports the engine's URL with the
        userinfo password masked, so it reads as an address and does not
        connect. A credential the engine carries as a query parameter is
        rendered as it is, so treat this as a credential and use
        `safe_url`, which redacts both.

        !!! warning
            The string may contain the password in the userinfo section
            (`postgresql://user:secret@host`). Treat the result as a
            credential. Do not log it. Use `safe_url` for any
            operator-facing output.
        """
        return self._url

    @property
    def safe_url(self) -> str:
        """Resolved Postgres URL with the password redacted.

        Safe to log or include in operator-facing diagnostics. The
        password is replaced with `***` whenever present.
        """
        return redact_url(self._url, multi_host=True)

    def __repr__(self) -> str:
        """Return a safe representation that never exposes the password."""
        cls = type(self).__name__
        return f"{cls}(url={self.safe_url!r})"

    @property
    def env_prefix(self) -> str:
        """Environment variable prefix used to resolve missing kwargs."""
        return self._env_prefix

    @property
    def command_timeout(self) -> float | None:
        """Per-operation timeout in seconds for pooled connections.

        Always None for a `from_client` or `from_engine` provider: the
        caller's pool carries its own timeout, which this provider does
        not read.
        """
        return self._command_timeout

    @property
    def client(self) -> Pool:
        """The underlying `asyncpg.Pool`.

        Raises:
            OutOfContextError: When accessed before `__aenter__`.
        """
        if self._pool is None:
            raise OutOfContextError(self, "client")
        return self._pool

    def lock(self, **kwargs: Any) -> PostgresLockAdapter:  # noqa: ANN401
        """Build a `PostgresLockAdapter` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresLockAdapter,
        )

        return PostgresLockAdapter(provider=self, **kwargs)

    def readwritelock(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> PostgresReadWriteLockAdapter:
        """Build a `PostgresReadWriteLockAdapter` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresReadWriteLockAdapter,
        )

        return PostgresReadWriteLockAdapter(provider=self, **kwargs)

    def leaderelection(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> PostgresLeaderElectionAdapter:
        """Build a `PostgresLeaderElectionAdapter` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresLeaderElectionAdapter,
        )

        return PostgresLeaderElectionAdapter(provider=self, **kwargs)

    def schedule(self, **kwargs: Any) -> PostgresScheduleAdapter:  # noqa: ANN401
        """Build a `PostgresScheduleAdapter` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresScheduleAdapter,
        )

        return PostgresScheduleAdapter(provider=self, **kwargs)

    def cache(self, **kwargs: Any) -> PostgresCacheAdapter:  # noqa: ANN401
        """Build a `PostgresCacheAdapter` bound to this provider."""
        from grelmicro.cache.postgres import (  # noqa: PLC0415
            PostgresCacheAdapter,
        )

        return PostgresCacheAdapter(provider=self, **kwargs)

    def outbox(self, **kwargs: Any) -> PostgresOutboxAdapter:  # noqa: ANN401
        """Build a `PostgresOutboxAdapter` bound to this provider."""
        from grelmicro.outbox.postgres import (  # noqa: PLC0415
            PostgresOutboxAdapter,
        )

        return PostgresOutboxAdapter(provider=self, **kwargs)

    def ratelimiter(self, **kwargs: Any) -> PostgresRateLimiterAdapter:  # noqa: ANN401
        """Build a `PostgresRateLimiterAdapter` bound to this provider."""
        from grelmicro.resilience.ratelimiter.postgres import (  # noqa: PLC0415
            PostgresRateLimiterAdapter,
        )

        return PostgresRateLimiterAdapter(provider=self, **kwargs)

    def circuitbreaker(self, **kwargs: Any) -> PostgresCircuitBreakerAdapter:  # noqa: ANN401
        """Build a `PostgresCircuitBreakerAdapter` bound to this provider."""
        from grelmicro.resilience.circuitbreaker.postgres import (  # noqa: PLC0415
            PostgresCircuitBreakerAdapter,
        )

        return PostgresCircuitBreakerAdapter(provider=self, **kwargs)

    async def check(self) -> None:
        """Run `SELECT 1` to prove the pool can serve a connection."""
        await self.client.fetchval("SELECT 1")

    def instrument(self, tracer_provider: Any) -> bool:  # noqa: ANN401
        """Attach the asyncpg OpenTelemetry instrumentor.

        asyncpg has no per-pool API, so this patches `asyncpg.Connection` once
        per process. The class-level guard keeps a second Postgres provider
        from double-instrumenting. Returns `False` when
        `opentelemetry-instrumentation-asyncpg` is not installed.
        """
        if PostgresProvider._asyncpg_instrumented:
            return True
        try:
            from opentelemetry.instrumentation.asyncpg import (  # noqa: PLC0415
                AsyncPGInstrumentor,
            )
        except ImportError:  # pragma: no cover
            return False
        AsyncPGInstrumentor().instrument(tracer_provider=tracer_provider)
        PostgresProvider._asyncpg_instrumented = True
        return True

    def uninstrument(self) -> None:
        """Reverse the process-wide asyncpg patch."""
        if not PostgresProvider._asyncpg_instrumented:
            return
        from opentelemetry.instrumentation.asyncpg import (  # noqa: PLC0415
            AsyncPGInstrumentor,
        )

        AsyncPGInstrumentor().uninstrument()
        PostgresProvider._asyncpg_instrumented = False

    async def __aenter__(self) -> Self:
        """Open the asyncpg pool when the provider owns it.

        A provider built from an engine rebuilds its facade over that
        same engine, so re-entering it never opens the private pool
        `from_engine` exists to avoid.
        """
        if self._pool is not None:
            return self
        if self._engine is not None:
            self._pool = cast("Pool", EnginePool(self._engine))
            return self
        self._pool = await create_pool(
            self._url, command_timeout=self._command_timeout
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the pool when the provider owns it.

        A borrowed engine is left open, and anything still checked out of
        it is handed back so the application can dispose it cleanly.
        """
        if self._own and self._pool is not None:
            await self._pool.close()
            self._pool = None
        elif isinstance(self._pool, EnginePool):
            await self._pool.release_all()


def _resolve_command_timeout(
    command_timeout: float | None,
    *,
    env_prefix: str,
    env_load: bool,
) -> float | None:
    """Resolve the command timeout from the kwarg or the environment.

    The kwarg wins. When it is None and `env_load` is True, read
    `{env_prefix}COMMAND_TIMEOUT`. Returns None when neither is set.
    """
    if command_timeout is not None:
        return command_timeout
    if not env_load:
        return None
    try:
        settings = _PostgresTimeoutEnvSettings(_env_prefix=env_prefix)
    except ValidationError as error:
        raise SettingsValidationError(error) from None
    return settings.command_timeout


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
    """Resolve the connection URL from kwargs and (optionally) the environment.

    A URL passed here is validated against `_PostgresEnvSettings.url`, the same
    type the environment path uses, so both accept the same URLs and fail the
    same way.
    """
    if url is not None and host is not None:
        msg = "pass either `url` or `host`, not both"
        raise SettingsValidationError(msg)

    if url is not None:
        try:
            validated = validate_url(
                str(url), settings_cls=_PostgresEnvSettings
            )
        except ValidationError as error:
            raise SettingsValidationError(error) from None
        return _normalize_scheme(validated)

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
        raise SettingsValidationError(msg)

    try:
        # `_env_prefix` is a pydantic-settings runtime kwarg that overrides
        # `model_config["env_prefix"]` per call. The stubs do not expose it,
        # so static checkers reject it even though the runtime accepts it.
        settings = _PostgresEnvSettings(_env_prefix=env_prefix)
    except ValidationError as error:
        raise SettingsValidationError(error) from None

    if settings.url is not None and settings.host is not None:
        msg = f"set either {env_prefix}URL or {env_prefix}HOST, not both"
        raise SettingsValidationError(msg)
    if settings.url is not None:
        return _normalize_scheme(
            settings.url.get_secret_value().unicode_string()
        )
    if settings.host is not None:
        return _compose_url(
            host=settings.host,
            port=settings.port,
            database=settings.db or settings.database,
            user=settings.user,
            password=(
                settings.password.get_secret_value()
                if settings.password
                else None
            ),
        )
    msg = f"either {env_prefix}URL or {env_prefix}HOST must be set"
    raise SettingsValidationError(msg)


_POSTGRESQL_SCHEME = "postgresql"
"""Scheme every driver-qualified Postgres URL normalises to."""

_DRIVER_PREFIX = f"{_POSTGRESQL_SCHEME}+"
"""Prefix marking a SQLAlchemy driver-qualified scheme."""


def _normalize_scheme(url: str) -> str:
    """Drop a SQLAlchemy driver suffix from the URL scheme.

    `PostgresDsn` accepts nine schemes, seven of them driver-qualified,
    so a `postgresql+asyncpg://` URL from a SQLAlchemy app validates and
    then fails at connect time. The suffix names that app's client
    library, not the wire protocol, and this provider always connects
    with asyncpg, so it is dropped rather than rejected.

    Only a `postgresql+` prefix is rewritten, so a string that is not a
    Postgres URL is returned untouched rather than truncated at some
    unrelated `+`.
    """
    scheme, separator, rest = url.partition("://")
    # A URL scheme is case-insensitive, so match on the folded form and
    # emit the lowercase spelling asyncpg expects.
    if not separator or not scheme.lower().startswith(_DRIVER_PREFIX):
        return url
    return f"{_POSTGRESQL_SCHEME}{separator}{rest}"


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
