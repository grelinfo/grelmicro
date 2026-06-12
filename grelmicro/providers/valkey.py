"""Valkey Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisConfig, RedisProvider, _resolve_url

if TYPE_CHECKING:
    from types import TracebackType

    from pydantic import RedisDsn

    from grelmicro.cache.redis import RedisCacheAdapter
    from grelmicro.coordination.redis import (
        RedisLeaderElectionBackend,
        RedisLockAdapter,
        RedisScheduleAdapter,
    )
    from grelmicro.resilience.circuitbreaker.redis import (
        RedisCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.redis import (
        RedisRateLimiterAdapter,
    )


class ValkeyProvider(RedisProvider):
    """Valkey connection provider.

    Holds the resolved URL and an async Valkey client. Adapters
    (`RedisLockAdapter`, `RedisCacheAdapter`, ...) borrow the client
    from a provider instead of opening their own pool, so multiple
    components against the same Valkey share one connection.

    `ValkeyProvider` is a subclass of `RedisProvider`. All Redis adapters
    accept it in place of a `RedisProvider`. The underlying client comes
    from `valkey.asyncio` instead of `redis.asyncio`.

    Construction forms (FastStream-style):

    ```python
    ValkeyProvider("redis://localhost:6379")     # positional URL
    ValkeyProvider(url="redis://...")            # keyword URL
    ValkeyProvider(host="x", port=6379, db=0)   # decomposed kwargs
    ValkeyProvider()                             # env-driven (VALKEY_*)
    ValkeyProvider(env_prefix="CACHE_VALKEY_")  # custom env prefix
    ValkeyProvider.from_config(RedisConfig(...))  # from a config object
    ValkeyProvider.from_client(client)           # bring-your-own client
    ```

    The provider is an async context manager: enter it to open the
    Valkey client, exit to close it. Adapters delegate their lifecycle
    to the provider when one is supplied.

    Read more in the [Providers](../providers.md) docs.
    """

    short_name: ClassVar[str] = "valkey"

    _valkey_bound: ClassVar[bool] = False

    def __init__(
        self,
        url: Annotated[
            RedisDsn | str | None,
            Doc(
                """
                The Valkey URL. Mutually exclusive with `host`.
                """,
            ),
        ] = None,
        *,
        host: Annotated[
            str | None,
            Doc("Valkey host. Mutually exclusive with `url`."),
        ] = None,
        port: Annotated[int | None, Doc("Valkey port.")] = None,
        db: Annotated[int | None, Doc("Valkey database index.")] = None,
        password: Annotated[str | None, Doc("Valkey password.")] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix. Defaults to `VALKEY_` so
                `VALKEY_URL`, `VALKEY_HOST`, ... are read out of the box.
                Override to split pools: `CACHE_VALKEY_`, `SESSION_VALKEY_`.
                """,
            ),
        ] = "VALKEY_",
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
        self._bind_valkey_classes()
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
        self._sentinel = None
        self._client = self._build_client(self._url)
        self._own = True

    @classmethod
    def _bind_valkey_classes(cls) -> None:
        """Swap the client classes to their `valkey.asyncio` equivalents.

        Done lazily so importing this module never imports `valkey`.
        `redis+sentinel://` and `redis+cluster://` URLs build a Valkey
        Sentinel or Cluster client exactly as `RedisProvider` does for
        Redis.
        """
        if cls.__dict__.get("_valkey_bound"):
            return
        from valkey.asyncio.client import Valkey  # noqa: PLC0415
        from valkey.asyncio.cluster import ValkeyCluster  # noqa: PLC0415
        from valkey.asyncio.sentinel import Sentinel  # noqa: PLC0415

        cls._redis_class = Valkey
        cls._cluster_class = ValkeyCluster
        cls._sentinel_class = Sentinel
        cls._valkey_bound = True

    @property
    def is_cluster(self) -> bool:
        """Whether the underlying client is a Valkey Cluster client."""
        return isinstance(self._client, self._cluster_class)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            RedisConfig,
            Doc("Pre-built `RedisConfig` carrying the connection settings."),
        ],
        *,
        env_prefix: str = "VALKEY_",
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
    def from_client(  # type: ignore[override]
        cls,
        client: Annotated[  # noqa: ANN401
            Any,
            Doc("A pre-built `valkey.asyncio.Valkey` client."),
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

        Use this when you already own a Valkey client (custom retry,
        sentinel, auth, testcontainers fixture, ...) and want
        grelmicro components to use it.
        """
        cls._bind_valkey_classes()
        self = cls.__new__(cls)
        self._env_prefix = "VALKEY_"
        self._url = ""
        self._sentinel = None
        self._client = client
        self._own = own
        return self

    @classmethod
    def sentinel(cls, **kwargs: Any) -> Self:  # noqa: ANN401
        """Build a provider backed by Valkey Sentinel."""
        cls._bind_valkey_classes()
        return super().sentinel(**kwargs)

    @classmethod
    def cluster(cls, **kwargs: Any) -> Self:  # noqa: ANN401
        """Build a provider backed by a Valkey Cluster."""
        cls._bind_valkey_classes()
        return super().cluster(**kwargs)

    def lock(self, **kwargs: Any) -> RedisLockAdapter:  # noqa: ANN401
        """Build a `RedisLockAdapter` bound to this provider."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisLockAdapter,
        )

        return RedisLockAdapter(provider=self, **kwargs)

    def leaderelection(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> RedisLeaderElectionBackend:
        """Build a `RedisLeaderElectionBackend` bound to this provider."""
        from grelmicro.coordination.redis import (  # noqa: PLC0415
            RedisLeaderElectionBackend,
        )

        return RedisLeaderElectionBackend(provider=self, **kwargs)

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
