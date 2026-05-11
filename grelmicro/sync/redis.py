"""Redis Synchronization Adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider
from grelmicro.sync.abc import SyncBackend

if TYPE_CHECKING:
    from types import TracebackType


class RedisSyncAdapter(SyncBackend):
    """Redis Synchronization Adapter.

    Wraps a `RedisProvider` and implements the `SyncBackend` protocol
    for distributed locks. Pass an explicit `provider=` to share a
    pool with other components, or rely on the default `env_prefix=`
    to build one from environment variables.
    """

    _LUA_ACQUIRE_OR_EXTEND = """
        local token = redis.call('get', KEYS[1])
        if not token then
            redis.call('set', KEYS[1], ARGV[1], 'px', ARGV[2])
            return 1
        end
        if token == ARGV[1] then
            redis.call('pexpire', KEYS[1], ARGV[2])
            return 1
        end
        return 0
    """
    _LUA_RELEASE = """
        local token = redis.call('get', KEYS[1])
        if not token or token ~= ARGV[1] then
            return 0
        end
        redis.call('del', KEYS[1])
        return 1
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
            Doc("Prefix prepended to every Redis key (lock isolation)."),
        ] = "",
    ) -> None:
        """Initialize the adapter."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._key_prefix = prefix
        self._bind_scripts()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _bind_scripts(self) -> None:
        """(Re)register the Lua scripts against the current client."""
        client = self._provider.client
        self._lua_release = client.register_script(self._LUA_RELEASE)
        self._lua_acquire = client.register_script(self._LUA_ACQUIRE_OR_EXTEND)

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False
        self._bind_scripts()

    async def __aenter__(self) -> Self:
        """Open the adapter and its provider when owned."""
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
        """Close the provider when owned. External providers are left alone."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire the lock."""
        return bool(
            await self._lua_acquire(
                keys=[f"{self._key_prefix}{name}"],
                args=[token, int(duration * 1000)],
                client=self._provider.client,
            )
        )

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        return bool(
            await self._lua_release(
                keys=[f"{self._key_prefix}{name}"],
                args=[token],
                client=self._provider.client,
            )
        )

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        return bool(
            await self._provider.client.get(f"{self._key_prefix}{name}")
        )

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        return (
            await self._provider.client.get(f"{self._key_prefix}{name}")
        ) == token.encode()
