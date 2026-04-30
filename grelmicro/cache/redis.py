"""Redis Cache Backend."""

from types import TracebackType
from typing import Annotated, Any, Self

from pydantic import RedisDsn
from typing_extensions import Doc

from grelmicro._redis import _create_redis_client
from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.errors import CacheSettingsValidationError

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
                Prefix prepended to all Redis keys to avoid
                conflicts with other keys.

                By default no prefix is added.
                """),
        ] = "",
    ) -> None:
        """Initialize the Redis cache backend."""
        self._url, self._redis = _create_redis_client(
            url, CacheSettingsValidationError
        )
        self._prefix = prefix

    async def __aenter__(self) -> Self:
        """Open the cache connection and register the backend as default."""
        cache_backend_registry.register(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the cache connection and unregister the backend."""
        await self._redis.aclose()
        cache_backend_registry.unregister(self)

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
