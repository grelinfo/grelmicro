"""Redis coordination adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.coordination.abc import (
    LeaderRecord,
    LockBackend,
    ScheduleBackend,
)
from grelmicro.providers.redis import RedisProvider, require_cluster_hash_tag

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


class RedisLockAdapter(LockBackend):
    """Redis Lock Adapter.

    Wraps a `RedisProvider` and implements the `LockBackend` protocol
    for distributed locks. Pass an explicit `provider=` to share a
    pool with other components, or rely on the default `env_prefix=`
    to build one from environment variables.

    Fencing tokens come from a Redis `INCR` on a per-name counter key,
    bumped inside the acquire Lua script only on a free-to-held transition.
    The lock value stores the token's fence so an extend by the same holder
    returns the same token. The counter key is never deleted on release, so
    re-acquire keeps climbing. Tokens are strictly monotonic against a single
    Redis master.
    """

    _LUA_ACQUIRE_OR_EXTEND = """
        -- KEYS[1] = lock key, KEYS[2] = fence counter key
        -- ARGV[1] = token, ARGV[2] = duration in ms
        -- The lock value is stored as "<fence>:<token>". The fence counter
        -- key is INCR'd only on a free-to-held transition and never deleted,
        -- so fencing tokens stay strictly monotonic across release cycles.
        local stored = redis.call('get', KEYS[1])
        if not stored then
            local fence = redis.call('incr', KEYS[2])
            redis.call(
                'set', KEYS[1], fence .. ':' .. ARGV[1], 'px', ARGV[2]
            )
            return fence
        end
        local sep = string.find(stored, ':', 1, true)
        local fence = tonumber(string.sub(stored, 1, sep - 1))
        local token = string.sub(stored, sep + 1)
        if token == ARGV[1] then
            redis.call('pexpire', KEYS[1], ARGV[2])
            return fence
        end
        return nil
    """
    _LUA_RELEASE = """
        -- The fence counter key is left untouched so re-acquire keeps climbing.
        local stored = redis.call('get', KEYS[1])
        if not stored then
            return 0
        end
        local sep = string.find(stored, ':', 1, true)
        local token = string.sub(stored, sep + 1)
        if token ~= ARGV[1] then
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
        require_cluster_hash_tag(
            self._provider, self._key_prefix, adapter="RedisLockAdapter"
        )
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

    def _fence_key(self, name: str) -> str:
        """Return the persistent fence counter key for a lock name."""
        return f"{self._key_prefix}fence:{name}"

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire the lock, returning the fencing token or `None`."""
        fence = await self._lua_acquire(
            keys=[f"{self._key_prefix}{name}", self._fence_key(name)],
            args=[token, int(duration * 1000)],
            client=self._provider.client,
        )
        return int(fence) if fence is not None else None

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
        stored = await self._provider.client.get(f"{self._key_prefix}{name}")
        if stored is None:
            return False
        value = stored.decode() if isinstance(stored, bytes) else stored
        _fence, sep, holder = value.partition(":")
        return sep == ":" and holder == token


class RedisScheduleAdapter(ScheduleBackend):
    """Redis Schedule Adapter.

    Wraps a `RedisProvider` and implements the `ScheduleBackend` protocol for
    durable distributed cron. The `last_fired` epoch is stored in a Redis
    string, and the claim decision runs server-side in a Lua script, so the
    compare-and-set is atomic across processes and machines.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment variables.
    """

    _LUA_CLAIM = """
        -- KEYS[1] = last_fired key
        -- ARGV[1] = due epoch
        -- Set last_fired to due only when absent or strictly less than due.
        local stored = redis.call('get', KEYS[1])
        if stored and tonumber(stored) >= tonumber(ARGV[1]) then
            return 0
        end
        redis.call('set', KEYS[1], ARGV[1])
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
            Doc("Prefix prepended to every Redis key (schedule isolation)."),
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
        self._lua_claim = client.register_script(self._LUA_CLAIM)

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

    def _key(self, name: str) -> str:
        """Return the Redis key for a schedule name."""
        return f"{self._key_prefix}{name}"

    async def claim(self, name: str, due: float) -> bool:
        """Atomically claim the fire at `due`."""
        return bool(
            await self._lua_claim(
                keys=[self._key(name)],
                args=[due],
                client=self._provider.client,
            )
        )

    async def last_fired(self, name: str) -> float | None:
        """Return the stored `last_fired` epoch, or `None`."""
        stored = await self._provider.client.get(self._key(name))
        if stored is None:
            return None
        return float(stored)


class RedisLeaderElectionBackend:
    """Redis leader election backend.

    Wraps a `RedisProvider` and implements the `LeaderElectionBackend`
    protocol. The `LeaderRecord` is stored in a Redis HASH and the
    acquire-or-renew decision runs server-side in a Lua script, so it is
    atomic across processes and machines.

    Expiry is computed inside Lua from the stored `renewed_at` plus
    `lease_duration` against the Redis server clock, not a key TTL. The
    expired record is kept on purpose so a takeover by a different holder
    can increment `transitions`.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment
    variables.
    """

    _LUA_ACQUIRE_OR_RENEW = """
        local key = KEYS[1]
        local token = ARGV[1]
        local duration = tonumber(ARGV[2])
        local metadata = ARGV[3]

        local now_pair = redis.call('TIME')
        local now = now_pair[1] + (now_pair[2] / 1000000)

        local stored = redis.call(
            'HMGET', key,
            'holder', 'lease_duration', 'acquired_at',
            'renewed_at', 'transitions', 'metadata'
        )

        if stored[1] == false then
            -- No record ever existed: acquire fresh.
            redis.call(
                'HSET', key,
                'holder', token,
                'lease_duration', tostring(duration),
                'acquired_at', tostring(now),
                'renewed_at', tostring(now),
                'transitions', '0',
                'metadata', metadata
            )
            return redis.call(
                'HMGET', key,
                'holder', 'lease_duration', 'acquired_at',
                'renewed_at', 'transitions', 'metadata'
            )
        end

        local holder = stored[1]
        local prev_acquired_at = stored[3]
        local prev_renewed_at = tonumber(stored[4])
        local prev_lease = tonumber(stored[2])
        local prev_transitions = tonumber(stored[5])
        local live = now < (prev_renewed_at + prev_lease)

        if live and holder ~= token then
            -- Someone else holds a valid lease: return their record.
            return stored
        end

        if live then
            -- Same holder renews: move renewed_at, keep acquired_at and
            -- transitions.
            redis.call(
                'HSET', key,
                'lease_duration', tostring(duration),
                'renewed_at', tostring(now),
                'metadata', metadata
            )
            return redis.call(
                'HMGET', key,
                'holder', 'lease_duration', 'acquired_at',
                'renewed_at', 'transitions', 'metadata'
            )
        end

        -- Expired record: acquire. Same holder keeps transitions, a
        -- different holder increments them.
        local transitions = prev_transitions
        if holder ~= token then
            transitions = prev_transitions + 1
        end
        redis.call(
            'HSET', key,
            'holder', token,
            'lease_duration', tostring(duration),
            'acquired_at', tostring(now),
            'renewed_at', tostring(now),
            'transitions', tostring(transitions),
            'metadata', metadata
        )
        return redis.call(
            'HMGET', key,
            'holder', 'lease_duration', 'acquired_at',
            'renewed_at', 'transitions', 'metadata'
        )
    """
    _LUA_RELEASE = """
        local key = KEYS[1]
        local token = ARGV[1]

        local stored = redis.call(
            'HMGET', key, 'holder', 'lease_duration', 'renewed_at'
        )
        if stored[1] == false then
            return 0
        end

        local now_pair = redis.call('TIME')
        local now = now_pair[1] + (now_pair[2] / 1000000)
        local live = now < (tonumber(stored[3]) + tonumber(stored[2]))

        if live and stored[1] == token then
            redis.call('DEL', key)
            return 1
        end
        return 0
    """
    _LUA_GET = """
        local key = KEYS[1]
        local stored = redis.call(
            'HMGET', key,
            'holder', 'lease_duration', 'acquired_at',
            'renewed_at', 'transitions', 'metadata'
        )
        if stored[1] == false then
            return nil
        end

        local now_pair = redis.call('TIME')
        local now = now_pair[1] + (now_pair[2] / 1000000)
        local live = now < (tonumber(stored[4]) + tonumber(stored[2]))
        if not live then
            return nil
        end
        return stored
    """

    def __init__(
        self,
        *,
        provider: Annotated[
            RedisProvider | None,
            Doc(
                """
                A pre-built `RedisProvider`. When set, the backend
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
            Doc("Prefix prepended to every Redis key (election isolation)."),
        ] = "",
    ) -> None:
        """Initialize the backend."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._key_prefix = prefix
        self._bind_scripts()

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _bind_scripts(self) -> None:
        """(Re)register the Lua scripts against the current client."""
        client = self._provider.client
        self._lua_acquire = client.register_script(self._LUA_ACQUIRE_OR_RENEW)
        self._lua_release = client.register_script(self._LUA_RELEASE)
        self._lua_get = client.register_script(self._LUA_GET)

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False
        self._bind_scripts()

    async def __aenter__(self) -> Self:
        """Open the backend and its provider when owned."""
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

    def _key(self, name: str) -> str:
        """Return the Redis key for an election name."""
        return f"{self._key_prefix}{name}"

    @staticmethod
    def _to_record(raw: list[bytes | None]) -> LeaderRecord:
        """Build a `LeaderRecord` from a Redis HMGET result."""
        (
            holder,
            lease_duration,
            acquired_at,
            renewed_at,
            transitions,
            metadata,
        ) = raw
        return LeaderRecord(
            holder=_as_str(holder),
            lease_duration=float(_as_str(lease_duration)),
            acquired_at=datetime.fromtimestamp(
                float(_as_str(acquired_at)), tz=UTC
            ),
            renewed_at=datetime.fromtimestamp(
                float(_as_str(renewed_at)), tz=UTC
            ),
            transitions=int(_as_str(transitions)),
            metadata=json.loads(_as_str(metadata)),
        )

    async def acquire_or_renew(
        self,
        *,
        name: str,
        token: str,
        duration: float,
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        """Acquire or renew the lease, returning the resulting record."""
        raw = await self._lua_acquire(
            keys=[self._key(name)],
            args=[token, duration, json.dumps(dict(metadata or {}))],
            client=self._provider.client,
        )
        return self._to_record(raw)

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lease when held by `token`."""
        return bool(
            await self._lua_release(
                keys=[self._key(name)],
                args=[token],
                client=self._provider.client,
            )
        )

    async def get(self, *, name: str) -> LeaderRecord | None:
        """Return the current live record, or `None`."""
        raw = await self._lua_get(
            keys=[self._key(name)],
            client=self._provider.client,
        )
        if raw is None:
            return None
        return self._to_record(raw)


def _as_str(value: bytes | str | None) -> str:
    """Decode a Redis field value to `str`."""
    if isinstance(value, bytes):
        return value.decode()
    if value is None:
        msg = "unexpected missing field in stored leader record"
        raise ValueError(msg)
    return value
