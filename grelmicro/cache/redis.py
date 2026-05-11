"""Redis Cache Adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider

if TYPE_CHECKING:
    from types import TracebackType

_CLEAR_BATCH_SIZE = 1000


class RedisCacheAdapter:
    """Redis cache storage backend.

    Wraps a `RedisProvider` and implements the cache protocol:
    `get`, `set` (with per-entry TTL via `SET ... PX`), `delete`,
    and a prefix-scoped `clear`. Pass an explicit `provider=` to share a
    pool with other components, or rely on the default `env_prefix=`
    to build one from environment variables.
    """

    def __init__(
        self,
        *,
        provider: Annotated[
            RedisProvider | None,
            Doc(
                """
                A pre-built `RedisProvider`. When set, the adapter
                borrows the provider's client and does not manage
                its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `RedisProvider` when `provider` is not set. Defaults
                to `REDIS_`. Use a custom prefix to split pools.
                """,
            ),
        ] = "REDIS_",
        prefix: Annotated[
            str,
            Doc("Prefix prepended to every Redis key (cache namespace)."),
        ] = "",
    ) -> None:
        """Initialize the Redis cache backend."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._key_prefix = prefix
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the cache connection."""
        self._loop = asyncio.get_running_loop()
        if self._owns_provider:
            await self._provider.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        return await self._provider.client.get(f"{self._key_prefix}{key}")

    async def set(self, *, key: str, value: bytes, ttl: float) -> None:
        """Store raw bytes with a TTL in seconds."""
        await self._provider.client.set(
            f"{self._key_prefix}{key}", value, px=int(ttl * 1000)
        )

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent)."""
        await self._provider.client.delete(f"{self._key_prefix}{key}")

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Uses SCAN to iterate keys without blocking Redis, then
        deletes in batches.
        """
        batch: list[Any] = []
        async for redis_key in self._provider.client.scan_iter(
            match=f"{self._key_prefix}*"
        ):
            batch.append(redis_key)
            if len(batch) >= _CLEAR_BATCH_SIZE:
                await self._provider.client.delete(*batch)
                batch = []
        if batch:
            await self._provider.client.delete(*batch)
