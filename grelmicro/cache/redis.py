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


class RedisCacheBackend:
    """Redis cache storage backend.

    Pure key-value storage with per-entry TTL handled natively
    by Redis (SETEX). Keys are prefixed for isolation.

    Must be used as an async context manager to manage the
    connection lifecycle.
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
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register this cache backend in the "
                "backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the Redis cache backend."""
        self._url = url or _get_redis_url()
        self._redis: Redis = Redis.from_url(str(self._url))
        self._prefix = prefix
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

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        return await self._redis.get(f"{self._prefix}{key}")

    async def set(self, *, key: str, value: bytes, ttl: float) -> None:
        """Store raw bytes with a TTL in seconds."""
        await self._redis.set(f"{self._prefix}{key}", value, px=int(ttl * 1000))

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent)."""
        await self._redis.delete(f"{self._prefix}{key}")

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Uses SCAN to iterate keys without blocking Redis, then
        deletes in batches.
        """
        batch: list[Any] = []
        async for redis_key in self._redis.scan_iter(match=f"{self._prefix}*"):
            batch.append(redis_key)
            if len(batch) >= _CLEAR_BATCH_SIZE:
                await self._redis.delete(*batch)
                batch = []
        if batch:
            await self._redis.delete(*batch)
