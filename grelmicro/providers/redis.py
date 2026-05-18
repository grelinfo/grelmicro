"""Redis Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from pydantic import BaseModel, RedisDsn, ValidationError
from pydantic_core import Url
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio.client import Redis
from typing_extensions import Doc

from grelmicro.errors import SettingsValidationError
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.cache.redis import RedisCacheAdapter
    from grelmicro.resilience.circuitbreaker.redis import (
        RedisCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.redis import (
        RedisRateLimiterAdapter,
    )
    from grelmicro.sync.redis import RedisSyncAdapter


class RedisConfig(BaseModel):
    """Redis connection settings.

    Plain `BaseModel` (env-free). Pass to `RedisProvider.from_config(cfg)`
    or build a `RedisProvider` directly from kwargs. The env path lives
    on the provider, not the config.
    """

    url: RedisDsn | None = None
    host: str | None = None
    port: int = 6379
    db: int = 0
    password: str | None = None


class _RedisEnvSettings(BaseSettings):
    """Read Redis settings from the environment (env_prefix-driven)."""

    model_config = SettingsConfigDict(extra="ignore")

    url: RedisDsn | None = None
    host: str | None = None
    port: int = 6379
    db: int = 0
    password: str | None = None


class RedisProvider(Provider):
    """Redis connection provider.

    Holds the resolved URL and an async Redis client. Adapters
    (`RedisSyncAdapter`, `RedisCacheAdapter`, ...) borrow the client
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
        self._url = _resolve_url(
            url=url,
            host=host,
            port=port,
            db=db,
            password=password,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        self._client: Redis[bytes] = Redis.from_url(self._url)
        self._own = True

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
            url=config.url.unicode_string() if config.url else None,
            host=config.host,
            port=config.port,
            db=config.db,
            password=config.password,
            env_prefix=env_prefix,
            env_load=False,
        )

    @classmethod
    def from_client(
        cls,
        client: Annotated[
            Redis[bytes],
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
        self._client = client
        self._own = own
        return self

    @property
    def url(self) -> str:
        """Resolved Redis URL (empty for `from_client` providers)."""
        return self._url

    @property
    def env_prefix(self) -> str:
        """Environment variable prefix used to resolve missing kwargs."""
        return self._env_prefix

    @property
    def client(self) -> Redis[bytes]:
        """The underlying `redis.asyncio.Redis` client."""
        return self._client

    def sync(self, **kwargs: Any) -> RedisSyncAdapter:  # noqa: ANN401
        """Build a `RedisSyncAdapter` bound to this provider."""
        from grelmicro.sync.redis import RedisSyncAdapter  # noqa: PLC0415

        return RedisSyncAdapter(provider=self, **kwargs)

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

    def breaker(self, **kwargs: Any) -> RedisCircuitBreakerAdapter:  # noqa: ANN401
        """Build a `RedisCircuitBreakerAdapter` bound to this provider."""
        from grelmicro.resilience.circuitbreaker.redis import (  # noqa: PLC0415
            RedisCircuitBreakerAdapter,
        )

        return RedisCircuitBreakerAdapter(provider=self, **kwargs)

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


def _resolve_url(
    *,
    url: RedisDsn | str | None,
    host: str | None,
    port: int | None,
    db: int | None,
    password: str | None,
    env_prefix: str,
    env_load: bool,
) -> str:
    """Resolve the connection URL from kwargs and (optionally) the environment."""
    if url is not None and host is not None:
        msg = "pass either `url` or `host`, not both"
        raise RedisProviderConfigError(msg)

    if url is not None:
        return str(url)

    if host is not None:
        return _compose_url(
            host=host, port=port or 6379, db=db or 0, password=password
        )

    if not env_load:
        msg = "no `url` or `host` provided and env_load is False"
        raise RedisProviderConfigError(msg)

    try:
        settings = _RedisEnvSettings(_env_prefix=env_prefix)  # type: ignore[call-arg]  # ty: ignore[unknown-argument]
    except ValidationError as error:
        raise RedisProviderConfigError(error) from None

    if settings.url is not None and settings.host is not None:
        msg = f"set either {env_prefix}URL or {env_prefix}HOST, not both"
        raise RedisProviderConfigError(msg)
    if settings.url is not None:
        return settings.url.unicode_string()
    if settings.host is not None:
        return _compose_url(
            host=settings.host,
            port=settings.port,
            db=settings.db,
            password=settings.password,
        )
    msg = f"either {env_prefix}URL or {env_prefix}HOST must be set"
    raise RedisProviderConfigError(msg)


def _compose_url(*, host: str, port: int, db: int, password: str | None) -> str:
    """Compose a `redis://` URL from decomposed parts."""
    return Url.build(
        scheme="redis",
        host=host,
        port=port,
        path=str(db),
        password=password,
    ).unicode_string()


class RedisProviderConfigError(SettingsValidationError):
    """Raised when the Redis provider cannot resolve a connection URL."""
