"""Redis Cache Backend."""

from types import TracebackType
from typing import Annotated, Any, Self

from pydantic import RedisDsn, ValidationError
from pydantic_core import Url
from pydantic_settings import BaseSettings
from redis.asyncio.client import Redis
from typing_extensions import Doc

from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.errors import CacheSettingsValidationError
from grelmicro.cache.ttl import CacheInfo


class _RedisSettings(BaseSettings):
    """Redis settings from the environment variables."""

    REDIS_HOST: str | None = None
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_URL: RedisDsn | None = None


def _get_redis_url() -> str:
    """Get the Redis URL from the environment variables.

    Raises:
        CacheSettingsValidationError: If the URL or host is not set.
    """
    try:
        settings = _RedisSettings()
    except ValidationError as error:
        raise CacheSettingsValidationError(error) from None

    if settings.REDIS_URL and not settings.REDIS_HOST:
        return settings.REDIS_URL.unicode_string()

    if settings.REDIS_HOST and not settings.REDIS_URL:
        return Url.build(
            scheme="redis",
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            path=str(settings.REDIS_DB),
            password=settings.REDIS_PASSWORD,
        ).unicode_string()

    if settings.REDIS_URL and settings.REDIS_HOST:
        msg = "Set either REDIS_URL or REDIS_HOST, not both"
    else:
        msg = "Either REDIS_URL or REDIS_HOST must be set"
    raise CacheSettingsValidationError(msg)


_CLEAR_BATCH_SIZE = 1000


class RedisCache:
    """Redis-backed async cache.

    Implements the ``AsyncCache`` protocol. Each entry is stored with
    a TTL handled natively by Redis. Keys are prefixed for isolation.

    Must be used as an async context manager to manage the connection
    lifecycle.

    Raises:
        ValueError: If ttl is not positive.
    """

    def __init__(
        self,
        url: Annotated[
            RedisDsn | str | None,
            Doc("""
                The Redis URL.

                If not provided, the URL will be taken from the
                environment variables REDIS_URL or REDIS_HOST,
                REDIS_PORT, REDIS_DB, and REDIS_PASSWORD.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                Prefix prepended to all Redis keys for isolation.

                By default no prefix is added.
                """),
        ] = "",
        ttl: Annotated[
            float,
            Doc("""
                Default TTL in seconds for all entries.
                """),
        ],
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register this cache backend in the "
                "backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the Redis cache."""
        if ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)

        self._url = url or _get_redis_url()
        self._redis: Redis = Redis.from_url(str(self._url))
        self._prefix = prefix
        self._ttl = ttl
        self._hits = 0
        self._misses = 0
        if auto_register:
            cache_backend_registry.set(self)

    async def __aenter__(self) -> Self:
        """Open the cache connection."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the cache connection."""
        await self._redis.aclose()

    async def get(
        self,
        key: str,
        default: Any = None,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Get a value by key.

        Returns the default if the key is missing or expired.
        """
        result = await self._redis.get(f"{self._prefix}{key}")
        if result is None:
            self._misses += 1
            return default
        self._hits += 1
        return result

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Set a value with the configured TTL."""
        await self._redis.set(f"{self._prefix}{key}", value, ex=self._ttl)

    async def delete(self, key: str) -> None:
        """Delete a key from the cache."""
        await self._redis.delete(f"{self._prefix}{key}")

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Uses SCAN to iterate keys without blocking Redis, then
        deletes in batches via pipeline.
        """
        batch: list[Any] = []
        async for redis_key in self._redis.scan_iter(match=f"{self._prefix}*"):
            batch.append(redis_key)
            if len(batch) >= _CLEAR_BATCH_SIZE:
                await self._redis.delete(*batch)
                batch = []
        if batch:
            await self._redis.delete(*batch)

    def cache_info(self) -> CacheInfo:
        """Return a snapshot of cache statistics.

        Counters are tracked locally (not stored in Redis).
        ``maxsize`` is always 0 (unlimited, managed by Redis).
        ``currsize`` is always 0 (counting prefixed keys is expensive).
        ``evictions`` is always 0 (managed by Redis eviction policy).
        """
        return CacheInfo(
            hits=self._hits,
            misses=self._misses,
            maxsize=0,
            currsize=0,
            evictions=0,
        )
