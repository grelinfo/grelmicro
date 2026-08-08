"""Redis coordination adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.coordination._protocol import (
    LeaderRecord,
    LockBackend,
    ReadWriteLockBackend,
    ReadWriteLockState,
    ScheduleBackend,
    WriteGrant,
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


class RedisReadWriteLockAdapter(ReadWriteLockBackend):
    """Redis Read-Write Lock Adapter.

    Wraps a `RedisProvider` and implements the `ReadWriteLockBackend`
    protocol. Every operation is one Lua script, so the reap, the decision,
    and the write apply atomically across processes and machines.

    Each lock name uses three keys: a hash holding the writer token, the
    writer's expiry, and the generation counter, a sorted set of reader
    tokens scored by their lease expiry, and a sorted set of writer intents
    scored the same way. Expiry is computed inside Lua against the Redis
    server clock rather than a key TTL, so an expired writer stays readable
    and the next writer learns that its predecessor died mid-write.

    Individual reader leases are what let a writer in promptly: the writer's
    own acquire drops the readers that expired instead of waiting for a
    whole-key TTL.
    """

    _LUA_NOW = """
        local t = redis.call('TIME')
        local now = t[1] * 1000 + math.floor(t[2] / 1000)
    """
    _LUA_REAP = """
        redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
        redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
    """

    _LUA_ACQUIRE_READ = (
        _LUA_NOW
        + _LUA_REAP
        + """
        local gen = tonumber(redis.call('HGET', KEYS[1], 'g')) or 0
        local exp = now + tonumber(ARGV[2])
        if redis.call('ZSCORE', KEYS[2], ARGV[1]) then
            redis.call('ZADD', KEYS[2], exp, ARGV[1])
            return gen
        end
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        if we and we > now then
            return nil
        end
        if redis.call('ZCARD', KEYS[3]) > 0 then
            return nil
        end
        redis.call('ZADD', KEYS[2], exp, ARGV[1])
        return gen
    """
    )

    _LUA_ACQUIRE_WRITE = (
        _LUA_NOW
        + _LUA_REAP
        + """
        local w = redis.call('HGET', KEYS[1], 'w')
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        local gen = tonumber(redis.call('HGET', KEYS[1], 'g')) or 0
        local exp = now + tonumber(ARGV[2])
        if w and we and we > now then
            if w == ARGV[1] then
                redis.call('HSET', KEYS[1], 'we', exp)
                return {gen, 0}
            end
            if ARGV[3] == '1' then
                redis.call('ZADD', KEYS[3], exp, ARGV[1])
            end
            return nil
        end
        if redis.call('ZCARD', KEYS[2]) > 0 then
            if ARGV[3] == '1' then
                redis.call('ZADD', KEYS[3], exp, ARGV[1])
            end
            return nil
        end
        local poisoned = 0
        if w then
            poisoned = 1
        end
        redis.call('ZREM', KEYS[3], ARGV[1])
        gen = gen + 1
        redis.call('HSET', KEYS[1], 'g', gen, 'w', ARGV[1], 'we', exp)
        return {gen, poisoned}
    """
    )

    _LUA_RELEASE_READ = (
        _LUA_NOW
        + _LUA_REAP
        + """
        return redis.call('ZREM', KEYS[2], ARGV[1])
    """
    )

    _LUA_RELEASE_WRITE = (
        _LUA_NOW
        + _LUA_REAP
        + """
        local w = redis.call('HGET', KEYS[1], 'w')
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        if w == ARGV[1] and we and we > now then
            redis.call('HDEL', KEYS[1], 'w', 'we')
            return 1
        end
        return 0
    """
    )

    _LUA_CANCEL_INTENT = (
        _LUA_NOW
        + _LUA_REAP
        + """
        return redis.call('ZREM', KEYS[3], ARGV[1])
    """
    )

    _LUA_DOWNGRADE = (
        _LUA_NOW
        + _LUA_REAP
        + """
        local w = redis.call('HGET', KEYS[1], 'w')
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        if w ~= ARGV[1] or not we or we <= now then
            return nil
        end
        local gen = tonumber(redis.call('HGET', KEYS[1], 'g')) or 0
        redis.call('HDEL', KEYS[1], 'w', 'we')
        redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), ARGV[1])
        return gen
    """
    )

    _LUA_STATE = (
        _LUA_NOW
        + _LUA_REAP
        + """
        local gen = tonumber(redis.call('HGET', KEYS[1], 'g')) or 0
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        local writing = 0
        if we and we > now then
            writing = 1
        end
        return {
            gen, writing,
            redis.call('ZCARD', KEYS[2]),
            redis.call('ZCARD', KEYS[3])
        }
    """
    )

    _LUA_OWNED_READ = (
        _LUA_NOW
        + _LUA_REAP
        + """
        if redis.call('ZSCORE', KEYS[2], ARGV[1]) then
            return 1
        end
        return 0
    """
    )

    _LUA_OWNED_WRITE = (
        _LUA_NOW
        + """
        local w = redis.call('HGET', KEYS[1], 'w')
        local we = tonumber(redis.call('HGET', KEYS[1], 'we'))
        if w == ARGV[1] and we and we > now then
            return 1
        end
        return 0
    """
    )

    def __init__(
        self,
        *,
        provider: Annotated[
            RedisProvider | None,
            Doc(
                """
                A pre-built `RedisProvider`. When set, the adapter borrows
                the provider's client and does not manage its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `RedisProvider` when `provider` is not set. Defaults to
                `REDIS_`. Use a custom prefix to split pools.
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
            self._provider,
            self._key_prefix,
            adapter="RedisReadWriteLockAdapter",
        )
        client = self._provider.client
        self._lua_acquire_read = client.register_script(self._LUA_ACQUIRE_READ)
        self._lua_acquire_write = client.register_script(
            self._LUA_ACQUIRE_WRITE
        )
        self._lua_release_read = client.register_script(self._LUA_RELEASE_READ)
        self._lua_release_write = client.register_script(
            self._LUA_RELEASE_WRITE
        )
        self._lua_cancel_intent = client.register_script(
            self._LUA_CANCEL_INTENT
        )
        self._lua_downgrade = client.register_script(self._LUA_DOWNGRADE)
        self._lua_state = client.register_script(self._LUA_STATE)
        self._lua_owned_read = client.register_script(self._LUA_OWNED_READ)
        self._lua_owned_write = client.register_script(self._LUA_OWNED_WRITE)

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

    def _keys(self, name: str) -> list[str]:
        """Return the hash, reader, and intent keys for a lock name."""
        base = f"{self._key_prefix}{name}"
        return [base, f"{base}:r", f"{base}:i"]

    async def acquire_read(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a read lease, returning the generation or `None`."""
        generation = await self._lua_acquire_read(
            keys=self._keys(name),
            args=[token, _duration_ms(duration)],
            client=self._provider.client,
        )
        return int(generation) if generation is not None else None

    async def acquire_write(
        self, *, name: str, token: str, duration: float, intent: bool = True
    ) -> WriteGrant | None:
        """Acquire the write lease, returning the grant or `None`."""
        result = await self._lua_acquire_write(
            keys=self._keys(name),
            args=[token, _duration_ms(duration), "1" if intent else "0"],
            client=self._provider.client,
        )
        if result is None:
            return None
        fence, poisoned = result
        return WriteGrant(fencing_token=int(fence), poisoned=bool(poisoned))

    async def release_read(self, *, name: str, token: str) -> bool:
        """Drop a read lease."""
        return bool(
            await self._lua_release_read(
                keys=self._keys(name),
                args=[token],
                client=self._provider.client,
            )
        )

    async def release_write(self, *, name: str, token: str) -> bool:
        """Drop the write lease, leaving the lock clean."""
        return bool(
            await self._lua_release_write(
                keys=self._keys(name),
                args=[token],
                client=self._provider.client,
            )
        )

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        """Withdraw a writer intent."""
        return bool(
            await self._lua_cancel_intent(
                keys=self._keys(name),
                args=[token],
                client=self._provider.client,
            )
        )

    async def downgrade(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Turn a held write lease into a read lease."""
        generation = await self._lua_downgrade(
            keys=self._keys(name),
            args=[token, _duration_ms(duration)],
            client=self._provider.client,
        )
        return int(generation) if generation is not None else None

    async def state(self, *, name: str) -> ReadWriteLockState:
        """Return a point-in-time view of the lock."""
        generation, writing, readers, intents = await self._lua_state(
            keys=self._keys(name),
            client=self._provider.client,
        )
        return ReadWriteLockState(
            generation=int(generation),
            writing=bool(writing),
            readers=int(readers),
            waiting_writers=int(intents),
        )

    async def owned_read(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds a live read lease."""
        return bool(
            await self._lua_owned_read(
                keys=self._keys(name),
                args=[token],
                client=self._provider.client,
            )
        )

    async def owned_write(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds the live write lease."""
        return bool(
            await self._lua_owned_write(
                keys=self._keys(name),
                args=[token],
                client=self._provider.client,
            )
        )


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


class RedisLeaderElectionAdapter:
    """Redis leader election adapter.

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


def _duration_ms(duration: float) -> int:
    """Return the lease duration in whole milliseconds, never zero.

    A duration under a millisecond would floor to zero, and a lease that
    expires the moment it is granted is never what a positive duration
    asked for.
    """
    return max(1, int(duration * 1000))


def _as_str(value: bytes | str | None) -> str:
    """Decode a Redis field value to `str`."""
    if isinstance(value, bytes):
        return value.decode()
    if value is None:
        msg = "unexpected missing field in stored leader record"
        raise ValueError(msg)
    return value
