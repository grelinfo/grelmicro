"""Redis Synchronization Backend."""

from types import TracebackType
from typing import Annotated, Self

from pydantic import RedisDsn
from typing_extensions import Doc

from grelmicro._redis import _create_redis_client
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import SyncSettingsValidationError


class RedisSyncBackend(SyncBackend):
    """Redis Synchronization Backend."""

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
        url: Annotated[
            RedisDsn | str | None,
            Doc("""
                The Redis URL.

                If not provided, the URL will be taken from the environment variables REDIS_URL
                or REDIS_HOST, REDIS_PORT, REDIS_DB, and REDIS_PASSWORD.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                The prefix to add on redis keys to avoid conflicts with other keys.

                By default no prefix is added.
                """),
        ] = "",
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register the lock backend in the backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the lock backend."""
        self._url, self._redis = _create_redis_client(
            url, SyncSettingsValidationError
        )
        self._prefix = prefix
        self._lua_release = self._redis.register_script(self._LUA_RELEASE)
        self._lua_acquire = self._redis.register_script(
            self._LUA_ACQUIRE_OR_EXTEND
        )
        if auto_register:
            sync_backend_registry.set(self)

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend."""
        await self._redis.aclose()

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire the lock."""
        return bool(
            await self._lua_acquire(
                keys=[f"{self._prefix}{name}"],
                args=[token, int(duration * 1000)],
                client=self._redis,
            )
        )

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        return bool(
            await self._lua_release(
                keys=[f"{self._prefix}{name}"], args=[token], client=self._redis
            )
        )

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        return bool(await self._redis.get(f"{self._prefix}{name}"))

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        return bool(
            (await self._redis.get(f"{self._prefix}{name}")) == token.encode()
        )  # redis returns bytes
