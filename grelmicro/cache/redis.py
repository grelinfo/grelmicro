"""Redis Cache Adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

_CLEAR_BATCH_SIZE = 1000
_TAG_SCAN_BATCH_SIZE = 500

# Atomic value write with tag association.
#
# KEYS[1]      value key
# KEYS[2]      reverse-tag set for the value key
# ARGV[1]      serialized value
# ARGV[2]      TTL in milliseconds
# ARGV[3..]    tag set keys to add the value key to
_SET_WITH_TAGS_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
local old = redis.call('SMEMBERS', KEYS[2])
for i = 1, #old do
    redis.call('SREM', old[i], KEYS[1])
end
redis.call('DEL', KEYS[2])
for i = 3, #ARGV do
    redis.call('SADD', ARGV[i], KEYS[1])
    redis.call('SADD', KEYS[2], ARGV[i])
end
redis.call('PEXPIRE', KEYS[2], ARGV[2])
return 1
"""

# Delete a value key and clean its reverse-tag membership.
#
# KEYS[1]      value key
# KEYS[2]      reverse-tag set for the value key
_DELETE_WITH_TAGS_SCRIPT = """
local tags = redis.call('SMEMBERS', KEYS[2])
for i = 1, #tags do
    redis.call('SREM', tags[i], KEYS[1])
end
redis.call('UNLINK', KEYS[1], KEYS[2])
return 1
"""

# Delete every member of one tag set, in bounded batches, then drop the
# set. SSCAN keeps memory bounded for large tags, and the whole script
# runs atomically so no member is missed by an interleaving write.
#
# KEYS[1]      tag set key
# ARGV[1]      SSCAN COUNT batch size
_DELETE_TAG_SCRIPT = """
local cursor = '0'
repeat
    local res = redis.call('SSCAN', KEYS[1], cursor, 'COUNT', ARGV[1])
    cursor = res[1]
    local members = res[2]
    if #members > 0 then
        redis.call('UNLINK', unpack(members))
    end
until cursor == '0'
redis.call('DEL', KEYS[1])
return 1
"""


class RedisCacheAdapter:
    """Redis cache storage backend.

    Wraps a `RedisProvider` and implements the cache protocol:
    `get`, `set` (with per-entry TTL via `SET ... PX`), `delete`,
    batch operations, tag-based invalidation, and a prefix-scoped
    `clear`. Pass an explicit `provider=` to share a pool with other
    components, or rely on the default `env_prefix=` to build one from
    environment variables.

    Each tag holds the fully qualified keys tagged with it. A reverse
    record next to each value lists the value's tags and self-expires
    with the value, so an expired key never leaves a stale tag entry
    behind. Both live under the cache prefix, so `clear` sweeps them too.
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

    def _full(self, key: str) -> str:
        """Return the Redis key for a fully qualified cache key."""
        return f"{self._key_prefix}{key}"

    def _tag_key(self, tag: str) -> str:
        """Return the Redis set key holding the members of a tag."""
        return f"{self._key_prefix}cache:tag:{tag}"

    def _rtag_key(self, key: str) -> str:
        """Return the Redis set key holding the tags of a value key."""
        return f"{self._key_prefix}cache:rtag:{key}"

    async def _run_script(
        self, script: str, numkeys: int, *args: str | bytes
    ) -> object:
        """Run a server-side script through the Redis client."""
        return await self._provider.client.eval(script, numkeys, *args)

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        return await self._provider.client.get(self._full(key))

    async def set(
        self,
        *,
        key: str,
        value: bytes,
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store raw bytes with a TTL in seconds and optional tags."""
        full_key = self._full(key)
        px = int(ttl * 1000)
        if not tags:
            await self._provider.client.set(full_key, value, px=px)
            return
        await self._run_script(
            _SET_WITH_TAGS_SCRIPT,
            2,
            full_key,
            self._rtag_key(key),
            value,
            str(px),
            *(self._tag_key(tag) for tag in tags),
        )

    async def get_many(self, *, keys: Sequence[str]) -> dict[str, bytes]:
        """Get raw bytes for many keys, returning only found entries."""
        keys = list(keys)
        if not keys:
            return {}
        values = await self._provider.client.mget(
            [self._full(key) for key in keys]
        )
        return {
            key: value
            for key, value in zip(keys, values, strict=True)
            if value is not None
        }

    async def set_many(
        self,
        *,
        items: Mapping[str, bytes],
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store many keys with one TTL and optional tags."""
        if not items:
            return
        px = int(ttl * 1000)
        tag_keys = [self._tag_key(tag) for tag in tags]
        if not tag_keys:
            pipe = self._provider.client.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(self._full(key), value, px=px)
            await pipe.execute()
            return
        for key, value in items.items():
            await self._run_script(
                _SET_WITH_TAGS_SCRIPT,
                2,
                self._full(key),
                self._rtag_key(key),
                value,
                str(px),
                *tag_keys,
            )

    async def delete(self, *, key: str) -> None:
        """Delete a key and clean its tag membership (no-op if absent)."""
        await self._run_script(
            _DELETE_WITH_TAGS_SCRIPT,
            2,
            self._full(key),
            self._rtag_key(key),
        )

    async def delete_many(self, *, keys: Sequence[str]) -> None:
        """Delete many keys and clean their tag membership."""
        for key in keys:
            await self.delete(key=key)

    async def delete_tags(self, *, tags: Sequence[str]) -> None:
        """Delete every key associated with any of the given tags."""
        for tag in tags:
            await self._run_script(
                _DELETE_TAG_SCRIPT,
                1,
                self._tag_key(tag),
                str(_TAG_SCAN_BATCH_SIZE),
            )

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Uses SCAN to iterate keys without blocking Redis, then
        deletes in batches. Tag and reverse-tag sets live under the
        same prefix, so this sweeps them too.
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
