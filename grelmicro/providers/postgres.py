"""Postgres Provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from asyncpg import Pool, create_pool
from pydantic import BaseModel, PostgresDsn, ValidationError
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Doc

from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.cache.postgres import PostgresCacheAdapter
    from grelmicro.coordination.postgres import (
        PostgresLeaderElectionBackend,
        PostgresLockAdapter,
    )
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
        """Resolved Postgres URL (empty for `from_client` providers).

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
        return _redact_url(self._url)

    def __repr__(self) -> str:
        """Return a safe representation that never exposes the password."""
        cls = type(self).__name__
        return f"{cls}(url={self.safe_url!r})"

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

    def lock(self, **kwargs: Any) -> PostgresLockAdapter:  # noqa: ANN401
        """Build a `PostgresLockAdapter` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresLockAdapter,
        )

        return PostgresLockAdapter(provider=self, **kwargs)

    def leader_election(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> PostgresLeaderElectionBackend:
        """Build a `PostgresLeaderElectionBackend` bound to this provider."""
        from grelmicro.coordination.postgres import (  # noqa: PLC0415
            PostgresLeaderElectionBackend,
        )

        return PostgresLeaderElectionBackend(provider=self, **kwargs)

    def cache(self, **kwargs: Any) -> PostgresCacheAdapter:  # noqa: ANN401
        """Build a `PostgresCacheAdapter` bound to this provider."""
        from grelmicro.cache.postgres import (  # noqa: PLC0415
            PostgresCacheAdapter,
        )

        return PostgresCacheAdapter(provider=self, **kwargs)

    def ratelimiter(self, **kwargs: Any) -> PostgresRateLimiterAdapter:  # noqa: ANN401
        """Build a `PostgresRateLimiterAdapter` bound to this provider."""
        from grelmicro.resilience.ratelimiter.postgres import (  # noqa: PLC0415
            PostgresRateLimiterAdapter,
        )

        return PostgresRateLimiterAdapter(provider=self, **kwargs)

    def breaker(self, **kwargs: Any) -> PostgresCircuitBreakerAdapter:  # noqa: ANN401
        """Build a `PostgresCircuitBreakerAdapter` bound to this provider."""
        from grelmicro.resilience.circuitbreaker.postgres import (  # noqa: PLC0415
            PostgresCircuitBreakerAdapter,
        )

        return PostgresCircuitBreakerAdapter(provider=self, **kwargs)

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
        # `_env_prefix` is a pydantic-settings runtime kwarg that overrides
        # `model_config["env_prefix"]` per call. The stubs do not expose it,
        # so static checkers reject it even though the runtime accepts it.
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


_USERINFO_RE = re.compile(r"(://|,)([^:@/?#,]*:)([^@/?#,]+)(@)")
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "auth",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "key",
    }
)


def _redact_query(query: str | None) -> str | None:
    """Return `query` with credential-like values replaced by `***`.

    Matches keys case-insensitively against `_CREDENTIAL_QUERY_KEYS`.
    Returns the input unchanged when no key matches.
    """
    if not query:
        return query
    from urllib.parse import parse_qsl, urlencode  # noqa: PLC0415

    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(k.lower() in _CREDENTIAL_QUERY_KEYS for k, _ in pairs):
        return query
    redacted = "***"
    redacted_pairs = [
        (k, redacted if k.lower() in _CREDENTIAL_QUERY_KEYS else v)
        for k, v in pairs
    ]
    # `safe="*"` keeps the `***` marker readable; other values are
    # properly escaped by `urlencode`.
    return urlencode(redacted_pairs, safe="*")


def _redact_url(url: str) -> str:
    """Redact userinfo password and credential-like query values with `***`.

    Tries structured parsing first. Falls back to a conservative regex
    on any parse failure so a malformed DSN still cannot leak the
    password through `safe_url` / `repr()`. Handles both single-host
    and multi-host Postgres DSNs.
    """
    if not url:
        return url
    try:
        parsed = MultiHostUrl(url)
    except ValueError:
        return _USERINFO_RE.sub(r"\1\2***\4", url)
    hosts = parsed.hosts()
    redacted_query = _redact_query(parsed.query)
    if (
        not any(h.get("password") for h in hosts)
        and redacted_query == parsed.query
    ):
        return url
    redacted = "***"
    redacted_hosts: list[Any] = []
    for h in hosts:
        entry: dict[str, Any] = {"host": h.get("host") or ""}
        if h.get("username"):
            entry["username"] = h["username"]
        if h.get("password"):
            entry["password"] = redacted
        port = h.get("port")
        if port is not None:
            entry["port"] = port
        redacted_hosts.append(entry)
    return MultiHostUrl.build(
        scheme=parsed.scheme,
        hosts=redacted_hosts,
        path=parsed.path.lstrip("/") if parsed.path else None,
        query=redacted_query,
        fragment=parsed.fragment,
    ).unicode_string()


class PostgresProviderConfigError(SettingsValidationError):
    """Raised when the Postgres provider cannot resolve a connection URL."""
