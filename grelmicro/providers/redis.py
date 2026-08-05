"""Redis Provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, RedisDsn, SecretStr, ValidationError
from pydantic.networks import UrlConstraints
from pydantic_core import MultiHostUrl, Url
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio.client import Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.sentinel import Sentinel
from typing_extensions import Doc

from grelmicro._redact import redact_url
from grelmicro.errors import (
    GrelmicroConfigWarning,
    SettingsValidationError,
)
from grelmicro.providers._base import Provider
from grelmicro.types import SecretUrl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from grelmicro.cache.redis import RedisCacheAdapter
    from grelmicro.coordination.redis import (
        RedisLeaderElectionAdapter,
        RedisLockAdapter,
        RedisScheduleAdapter,
    )
    from grelmicro.resilience.circuitbreaker.redis import (
        RedisCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.redis import (
        RedisRateLimiterAdapter,
    )


RedisAnyDsn = Annotated[
    MultiHostUrl,
    UrlConstraints(
        allowed_schemes=[
            "redis",
            "rediss",
            "unix",
            "redis+sentinel",
            "redis+cluster",
        ]
    ),
]
"""Every Redis URL scheme the provider understands.

Multi-host so `redis+sentinel://a:26379,b:26379/mymaster/0` and
`redis+cluster://a:6379,b:6379` validate, and the same type serves the
constructor, `RedisConfig`, and the environment so all three accept the same
URLs.
"""


class RedisConfig(BaseModel):
    """Redis connection settings.

    Plain `BaseModel` (env-free). Pass to `RedisProvider.from_config(cfg)`
    or build a `RedisProvider` directly from kwargs. The env path lives
    on the provider, not the config.

    A rejected value is never echoed back: a mistyped URL would carry the
    password into the `ValidationError` text.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    url: SecretUrl[RedisAnyDsn] | None = None
    host: str | None = None
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None
    sentinel_password: SecretStr | None = None


class _RedisEnvSettings(BaseSettings):
    """Read Redis settings from the environment (env_prefix-driven)."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    url: SecretUrl[RedisAnyDsn] | None = None
    host: str | None = None
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None
    sentinel_password: SecretStr | None = None


class RedisProvider(Provider):
    """Redis connection provider.

    Holds the resolved URL and an async Redis client. Adapters
    (`RedisLockAdapter`, `RedisCacheAdapter`, ...) borrow the client
    from a provider instead of opening their own pool, so multiple
    components against the same Redis share one connection.

    Construction forms (FastStream-style):

    ```python
    RedisProvider("redis://localhost:6379")     # positional URL
    RedisProvider(url="redis://...")            # keyword URL
    RedisProvider(host="x", port=6379, db=0)    # decomposed kwargs
    RedisProvider()                             # env-driven (REDIS_*)
    RedisProvider(env_prefix="CACHE_REDIS_")    # custom env prefix
    RedisProvider.from_config(RedisConfig(...)) # from a config object
    RedisProvider.from_client(client)           # bring-your-own client
    ```

    The provider is an async context manager: enter it to open the
    Redis client, exit to close it. Adapters delegate their lifecycle
    to the provider when one is supplied.

    Read more in the [Providers](../providers.md) docs.
    """

    short_name: ClassVar[str] = "redis"

    _redis_class: ClassVar[type[Any]] = Redis
    _cluster_class: ClassVar[type[Any]] = RedisCluster
    _sentinel_class: ClassVar[type[Any]] = Sentinel

    def __init__(
        self,
        url: Annotated[
            RedisDsn | str | None,
            Doc(
                """
                The Redis URL. Mutually exclusive with `host`.
                """,
            ),
        ] = None,
        *,
        host: Annotated[
            str | None,
            Doc("Redis host. Mutually exclusive with `url`."),
        ] = None,
        port: Annotated[int | None, Doc("Redis port.")] = None,
        db: Annotated[int | None, Doc("Redis database index.")] = None,
        password: Annotated[str | None, Doc("Redis password.")] = None,
        sentinel_password: Annotated[
            str | None,
            Doc(
                """
                Password for the Sentinel servers themselves, when they run
                with `requirepass`. Applies only to a `redis+sentinel://`
                URL, and only when set: `AUTH` against a server without
                `requirepass` fails, so the data password is never reused
                here.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix. Defaults to `REDIS_` so
                `REDIS_URL`, `REDIS_HOST`, ... are read out of the box.
                Override to split pools: `CACHE_REDIS_`, `SESSION_REDIS_`.
                """,
            ),
        ] = "REDIS_",
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
        self._url, resolved_sentinel_password = _resolve_url(
            url=url,
            host=host,
            port=port,
            db=db,
            password=password,
            sentinel_password=sentinel_password,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        self._sentinel: Sentinel | None = None
        self._client = self._build_client(
            self._url,
            sentinel_kwargs=_sentinel_kwargs(
                self._url, resolved_sentinel_password, env_prefix=env_prefix
            ),
        )
        self._instrumentor: Any = None
        self._own = True

    def _build_client(
        self,
        url: str,
        *,
        sentinel_kwargs: Mapping[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401
        """Build the async client matching the URL scheme.

        `redis://`, `rediss://`, and `unix://` open a plain client.
        `redis+sentinel://` opens a `Sentinel` and returns its master
        proxy. `redis+cluster://` opens a `RedisCluster`.
        """
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme == _SENTINEL_SCHEME:
            parts = _parse_multihost_url(url)
            self._sentinel = self._sentinel_class(
                parts.hosts,
                username=parts.username,
                password=parts.password,
                sentinel_kwargs=dict(sentinel_kwargs)
                if sentinel_kwargs is not None
                else None,
            )
            return self._sentinel.master_for(
                parts.service_name,
                db=parts.db,
                username=parts.username,
                password=parts.password,
            )
        if scheme == _CLUSTER_SCHEME:
            import importlib  # noqa: PLC0415

            cluster_node = importlib.import_module(
                self._cluster_class.__module__
            ).ClusterNode
            parts = _parse_multihost_url(url)
            first_host, first_port = parts.hosts[0]
            return self._cluster_class(
                host=first_host,
                port=first_port,
                startup_nodes=[
                    cluster_node(host, port) for host, port in parts.hosts
                ],
                username=parts.username,
                password=parts.password,
            )
        return self._redis_class.from_url(url)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            RedisConfig,
            Doc("Pre-built `RedisConfig` carrying the connection settings."),
        ],
        *,
        env_prefix: str = "REDIS_",
    ) -> Self:
        """Build a provider from a `RedisConfig` instance.

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
            db=config.db,
            password=(
                config.password.get_secret_value() if config.password else None
            ),
            sentinel_password=(
                config.sentinel_password.get_secret_value()
                if config.sentinel_password
                else None
            ),
            env_prefix=env_prefix,
            env_load=False,
        )

    @classmethod
    def from_client(
        cls,
        client: Annotated[
            Redis,
            Doc("A pre-built `redis.asyncio.Redis` client."),
        ],
        *,
        own: Annotated[
            bool,
            Doc(
                """
                When True, the provider closes the client on `__aexit__`.
                When False (default), the caller keeps ownership and
                must close the client themselves.
                """,
            ),
        ] = False,
    ) -> Self:
        """Build a provider that wraps an existing native client.

        Use this when you already own a Redis client (custom retry,
        sentinel, auth, testcontainers fixture, ...) and want
        grelmicro components to use it.
        """
        self = cls.__new__(cls)
        self._env_prefix = "REDIS_"
        self._url = ""
        self._sentinel = None
        self._client = client
        self._instrumentor = None
        self._own = own
        return self

    @classmethod
    def sentinel(
        cls,
        *,
        sentinels: Annotated[
            Sequence[tuple[str, int]],
            Doc("Sentinel `(host, port)` pairs to query for the master."),
        ],
        service_name: Annotated[
            str,
            Doc("The Sentinel master group name to resolve."),
        ],
        db: Annotated[int, Doc("Database index for data connections.")] = 0,
        password: Annotated[
            str | None,
            Doc(
                """
                Password for both the Sentinel and data connections. Use
                `sentinel_kwargs` when the Sentinel password differs.
                """,
            ),
        ] = None,
        sentinel_kwargs: Annotated[
            Mapping[str, Any] | None,
            Doc(
                """
                Extra keyword arguments passed to the Sentinel
                connections only (not the data connections). Use it for a
                distinct Sentinel password or TLS settings.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc("Environment variable prefix recorded on the provider."),
        ] = "REDIS_",
    ) -> Self:
        """Build a provider backed by Redis Sentinel.

        Resolves the master through Sentinel and routes every command to
        it. The client re-resolves the master on failover, so wrap calls
        in the resilience patterns to survive the brief window where
        in-flight commands can error.
        """
        url = _compose_sentinel_url(
            sentinels=sentinels,
            service_name=service_name,
            db=db,
            password=password,
        )
        self = cls.__new__(cls)
        self._env_prefix = env_prefix
        self._url = url
        self._sentinel = None
        self._client = self._build_client(url, sentinel_kwargs=sentinel_kwargs)
        self._instrumentor = None
        self._own = True
        return self

    @classmethod
    def cluster(
        cls,
        *,
        nodes: Annotated[
            Sequence[tuple[str, int]],
            Doc("Cluster seed `(host, port)` pairs for node discovery."),
        ],
        password: Annotated[
            str | None,
            Doc("Password applied to every cluster connection."),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc("Environment variable prefix recorded on the provider."),
        ] = "REDIS_",
    ) -> Self:
        """Build a provider backed by a Redis Cluster.

        The cluster client discovers the full topology from the seed
        nodes and routes each key to its owning slot. Multi-key
        operations must land in one slot: see the cache hash-tag rule in
        the [Providers](../providers.md) docs.
        """
        url = _compose_cluster_url(nodes=nodes, password=password)
        self = cls.__new__(cls)
        self._env_prefix = env_prefix
        self._url = url
        self._sentinel = None
        self._client = self._build_client(url)
        self._instrumentor = None
        self._own = True
        return self

    @property
    def url(self) -> str:
        """Resolved Redis URL (empty for `from_client` providers).

        !!! warning
            The string may contain the password in the userinfo section
            (`redis://:secret@host`). Treat the result as a credential.
            Do not log it. Use `safe_url` for any operator-facing output.
        """
        return self._url

    @property
    def safe_url(self) -> str:
        """Resolved Redis URL with the password redacted.

        Safe to log or include in operator-facing diagnostics. The
        password is replaced with `***` whenever present.
        """
        return redact_url(self._url)

    def __repr__(self) -> str:
        """Return a safe representation that never exposes the password."""
        cls = type(self).__name__
        return f"{cls}(url={self.safe_url!r})"

    @property
    def env_prefix(self) -> str:
        """Environment variable prefix used to resolve missing kwargs."""
        return self._env_prefix

    @property
    def is_cluster(self) -> bool:
        """Whether the underlying client is a Redis Cluster client.

        Adapters that run multi-key commands check this to enforce the
        single-slot hash-tag rule on Cluster.
        """
        return isinstance(self._client, RedisCluster)

    @property
    def client(self) -> Any:  # noqa: ANN401
        """The underlying async Redis client.

        A `redis.asyncio.Redis` for standalone and Sentinel URLs (the
        Sentinel form returns the master proxy), or a
        `redis.asyncio.cluster.RedisCluster` for cluster URLs.
        """
        return self._client

    def lock(self, **kwargs: Any) -> RedisLockAdapter:  # noqa: ANN401
        """Build a `RedisLockAdapter` bound to this provider."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisLockAdapter,
        )

        return RedisLockAdapter(provider=self, **kwargs)

    def leaderelection(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> RedisLeaderElectionAdapter:
        """Build a `RedisLeaderElectionAdapter` bound to this provider."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisLeaderElectionAdapter,
        )

        return RedisLeaderElectionAdapter(provider=self, **kwargs)

    def schedule(self, **kwargs: Any) -> RedisScheduleAdapter:  # noqa: ANN401
        """Build a `RedisScheduleAdapter` bound to this provider."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisScheduleAdapter,
        )

        return RedisScheduleAdapter(provider=self, **kwargs)

    def cache(self, **kwargs: Any) -> RedisCacheAdapter:  # noqa: ANN401
        """Build a `RedisCacheAdapter` bound to this provider."""
        from grelmicro.cache.redis import RedisCacheAdapter  # noqa: PLC0415

        return RedisCacheAdapter(provider=self, **kwargs)

    def ratelimiter(self, **kwargs: Any) -> RedisRateLimiterAdapter:  # noqa: ANN401
        """Build a `RedisRateLimiterAdapter` bound to this provider."""
        from grelmicro.resilience.ratelimiter.redis import (  # noqa: PLC0415
            RedisRateLimiterAdapter,
        )

        return RedisRateLimiterAdapter(provider=self, **kwargs)

    def circuitbreaker(self, **kwargs: Any) -> RedisCircuitBreakerAdapter:  # noqa: ANN401
        """Build a `RedisCircuitBreakerAdapter` bound to this provider."""
        from grelmicro.resilience.circuitbreaker.redis import (  # noqa: PLC0415
            RedisCircuitBreakerAdapter,
        )

        return RedisCircuitBreakerAdapter(provider=self, **kwargs)

    async def check(self) -> None:
        """`PING` Redis to prove the connection is reachable."""
        await self._client.ping()

    def instrument(self, tracer_provider: Any) -> bool:  # noqa: ANN401
        """Attach the Redis OpenTelemetry instrumentor to this client.

        Uses per-instance `instrument_client`, so only this provider's client
        is traced, not every Redis client in the process. Returns `False` when
        `opentelemetry-instrumentation-redis` is not installed.
        """
        try:
            from opentelemetry.instrumentation.redis import (  # noqa: PLC0415
                RedisInstrumentor,
            )
        except ImportError:  # pragma: no cover
            return False
        self._instrumentor = RedisInstrumentor()
        self._instrumentor.instrument_client(
            self._client, tracer_provider=tracer_provider
        )
        return True

    def uninstrument(self) -> None:
        """Detach the Redis instrumentor from this client."""
        if self._instrumentor is not None:
            self._instrumentor.uninstrument_client(self._client)
            self._instrumentor = None

    async def __aenter__(self) -> Self:
        """Open the provider. The client is already constructed eagerly."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when the provider owns it."""
        if self._own:
            await self._client.aclose()
            if self._sentinel is not None:
                for sentinel in self._sentinel.sentinels:
                    await sentinel.aclose()


def _resolve_url(
    *,
    url: RedisDsn | str | None,
    host: str | None,
    port: int | None,
    db: int | None,
    password: str | None,
    sentinel_password: str | None,
    env_prefix: str,
    env_load: bool,
) -> tuple[str, str | None]:
    """Resolve the connection URL and Sentinel password from kwargs and env."""
    if url is not None and host is not None:
        msg = "pass either `url` or `host`, not both"
        raise RedisProviderConfigError(msg)

    if url is not None:
        return (
            _apply_password(str(url), password, source="`password`"),
            sentinel_password,
        )

    if host is not None:
        return (
            _compose_url(
                host=host, port=port or 6379, db=db or 0, password=password
            ),
            sentinel_password,
        )

    if not env_load:
        msg = "no `url` or `host` provided and env_load is False"
        raise RedisProviderConfigError(msg)

    try:
        # `_env_prefix` is a pydantic-settings runtime kwarg that overrides
        # `model_config["env_prefix"]` per call. The stubs do not expose it,
        # so static checkers reject it even though the runtime accepts it.
        settings = _RedisEnvSettings(_env_prefix=env_prefix)  # ty: ignore[unknown-argument]
    except ValidationError as error:
        raise RedisProviderConfigError(error) from None

    if settings.url is not None and settings.host is not None:
        msg = f"set either {env_prefix}URL or {env_prefix}HOST, not both"
        raise RedisProviderConfigError(msg)
    # `is not None`, not `or`: an explicitly passed empty string is a value
    # the caller chose, and falling through to the environment would invert
    # the precedence every other field follows.
    resolved_sentinel = (
        sentinel_password
        if sentinel_password is not None
        else (
            settings.sentinel_password.get_secret_value()
            if settings.sentinel_password
            else None
        )
    )
    if settings.url is not None:
        return (
            _apply_password(
                settings.url.get_secret_value().unicode_string(),
                settings.password.get_secret_value()
                if settings.password
                else None,
                source=f"{env_prefix}PASSWORD",
            ),
            resolved_sentinel,
        )
    if settings.host is not None:
        return (
            _compose_url(
                host=settings.host,
                port=settings.port,
                db=settings.db,
                password=(
                    settings.password.get_secret_value()
                    if settings.password
                    else None
                ),
            ),
            resolved_sentinel,
        )
    msg = f"either {env_prefix}URL or {env_prefix}HOST must be set"
    raise RedisProviderConfigError(msg)


def _sentinel_kwargs(
    url: str, sentinel_password: str | None, *, env_prefix: str
) -> dict[str, Any] | None:
    """Build the Sentinel connection kwargs for a resolved URL.

    Returns `None` unless a Sentinel password is configured, so the argument
    is absent rather than empty for every other deployment.

    A Sentinel password with a non-Sentinel URL is reported rather than
    dropped. It is a warning and not an error because one environment often
    serves several services, only some of which talk to Sentinel, and the
    variable being present is not itself a mistake.
    """
    if sentinel_password is None:
        return None
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme != _SENTINEL_SCHEME:
        import warnings  # noqa: PLC0415

        msg = (
            f"a Sentinel password is configured but the URL scheme is "
            f"{scheme!r}, so it was not applied. It configures the Sentinel "
            f"servers, which only a {_SENTINEL_SCHEME}:// URL connects to. "
            f"Set it with `sentinel_password=` or "
            f"{env_prefix}SENTINEL_PASSWORD."
        )
        warnings.warn(msg, GrelmicroConfigWarning, stacklevel=4)
        return None
    return {"password": sentinel_password}


def _apply_password(url: str, password: str | None, *, source: str) -> str:
    """Fold a separately configured password into a URL that carries none.

    A URL in one variable and a password in another is the usual split on
    Kubernetes, where the host comes from a ConfigMap and the credential from
    a Secret. Both halves are kept.

    Raises:
        RedisProviderConfigError: If the URL already carries credentials and a
            password is configured too, since the two may disagree and picking
            one silently is how a wrong credential reaches the server.
    """
    if password is None:
        return url
    # Split properly rather than on the first `/`: a URL with a query and no
    # path would otherwise fold the query into the authority, and a query
    # value containing `@` would read as userinfo.
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    if "@" in parts.netloc:
        msg = (
            f"the Redis URL already carries credentials, so {source} is "
            f"ambiguous. Keep the password in the URL userinfo or drop it "
            f"from the URL, not both."
        )
        raise RedisProviderConfigError(msg)
    netloc = f":{quote(password, safe='')}@{parts.netloc}"
    return urlunsplit(parts._replace(netloc=netloc))


def _compose_url(*, host: str, port: int, db: int, password: str | None) -> str:
    """Compose a `redis://` URL from decomposed parts."""
    return Url.build(
        scheme="redis",
        host=host,
        port=port,
        path=str(db),
        password=password,
    ).unicode_string()


_SENTINEL_SCHEME = "redis+sentinel"
_CLUSTER_SCHEME = "redis+cluster"
_DEFAULT_SENTINEL_PORT = 26379
_DEFAULT_REDIS_PORT = 6379


class _MultiHostUrl:
    """Parsed parts of a multi-host `redis+sentinel`/`redis+cluster` URL."""

    __slots__ = ("db", "hosts", "password", "service_name", "username")

    def __init__(
        self,
        *,
        hosts: list[tuple[str, int]],
        username: str | None,
        password: str | None,
        service_name: str,
        db: int,
    ) -> None:
        self.hosts = hosts
        self.username = username
        self.password = password
        self.service_name = service_name
        self.db = db


def _parse_multihost_url(url: str) -> _MultiHostUrl:
    """Parse a `redis+sentinel`/`redis+cluster` multi-host URL.

    Authority is a comma-separated `host:port` list. For sentinel the
    first path segment is the master service name and an optional second
    segment is the database index. Userinfo credentials apply to every
    connection.
    """
    from urllib.parse import unquote, urlsplit  # noqa: PLC0415

    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    default_port = (
        _DEFAULT_SENTINEL_PORT
        if scheme == _SENTINEL_SCHEME
        else _DEFAULT_REDIS_PORT
    )

    authority, _, path = rest.partition("/")
    username: str | None = None
    password: str | None = None
    if "@" in authority:
        userinfo, _, authority = authority.rpartition("@")
        user_part, sep, pass_part = userinfo.partition(":")
        username = unquote(user_part) or None
        password = unquote(pass_part) if sep else None

    hosts: list[tuple[str, int]] = []
    for item in authority.split(","):
        if not item:
            continue
        parsed = urlsplit(f"//{item}")
        host = parsed.hostname
        if not host:
            msg = f"missing host in {scheme} URL authority: {item!r}"
            raise RedisProviderConfigError(msg)
        hosts.append((host, parsed.port or default_port))
    if not hosts:
        msg = f"{scheme} URL must list at least one host"
        raise RedisProviderConfigError(msg)

    service_name = ""
    db = 0
    if scheme == _SENTINEL_SCHEME:
        segments = [seg for seg in path.split("/") if seg]
        if not segments:
            msg = "redis+sentinel URL must name the master service"
            raise RedisProviderConfigError(msg)
        service_name = segments[0]
        if len(segments) > 1:
            try:
                db = int(segments[1])
            except ValueError:
                msg = f"invalid database index in URL: {segments[1]!r}"
                raise RedisProviderConfigError(msg) from None

    return _MultiHostUrl(
        hosts=hosts,
        username=username,
        password=password,
        service_name=service_name,
        db=db,
    )


def _compose_sentinel_url(
    *,
    sentinels: Sequence[tuple[str, int]],
    service_name: str,
    db: int,
    password: str | None,
) -> str:
    """Compose a `redis+sentinel://` URL from decomposed parts."""
    authority = ",".join(f"{host}:{port}" for host, port in sentinels)
    userinfo = f":{_quote_credential(password)}@" if password else ""
    return f"{_SENTINEL_SCHEME}://{userinfo}{authority}/{service_name}/{db}"


def _compose_cluster_url(
    *,
    nodes: Sequence[tuple[str, int]],
    password: str | None,
) -> str:
    """Compose a `redis+cluster://` URL from decomposed parts."""
    authority = ",".join(f"{host}:{port}" for host, port in nodes)
    userinfo = f":{_quote_credential(password)}@" if password else ""
    return f"{_CLUSTER_SCHEME}://{userinfo}{authority}"


def _quote_credential(value: str) -> str:
    """Percent-encode a credential for safe inclusion in URL userinfo."""
    from urllib.parse import quote  # noqa: PLC0415

    return quote(value, safe="")


_HASH_TAG_RE = re.compile(r"\{[^}]+\}")


def require_cluster_hash_tag(
    provider: RedisProvider, prefix: str, *, adapter: str
) -> None:
    """Validate that a multi-key adapter is cluster-safe.

    On a Redis Cluster, an adapter that touches several keys in one
    command or script must keep them in one slot. A hash tag in the
    prefix (`{myapp}cache`) forces every key under it into the same
    slot. Raises `ValueError` when the client is a cluster and the
    prefix has no `{...}` hash tag. No-op for standalone and Sentinel.
    """
    if provider.is_cluster and not _HASH_TAG_RE.search(prefix):
        msg = (
            f"{adapter} runs multi-key operations that fail cross-slot on a "
            f"Redis Cluster. Add a hash tag to the prefix so every key lands "
            f'in one slot, e.g. prefix="{{myapp}}{prefix or "cache"}".'
        )
        raise ValueError(msg)


class RedisProviderConfigError(SettingsValidationError):
    """Raised when the Redis provider cannot resolve a connection URL."""
