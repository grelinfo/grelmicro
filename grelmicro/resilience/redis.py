"""Redis Rate Limiter Backend."""

from types import TracebackType
from typing import Annotated, Any, Self, assert_never

from pydantic import RedisDsn
from redis.asyncio import Redis
from typing_extensions import Doc

from grelmicro._redis import _create_redis_client
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import (
    GCRAConfig,
    RateLimiterConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.errors import ResilienceSettingsValidationError


class RedisRateLimiterBackend(RateLimiterBackend):
    """Redis rate limiter backend.

    Supports both
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    and [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig]
    algorithm configs via atomic Lua scripts. Safe across processes
    and machines.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucketConfig
    from grelmicro.resilience.redis import RedisRateLimiterBackend


    async def main() -> None:
        async with RedisRateLimiterBackend("redis://localhost:6379/0"):
            rl = RateLimiter(
                "api",
                TokenBucketConfig(capacity=10, refill_rate=1),
            )
            await rl.acquire(key="u1")
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(
        self,
        url: Annotated[
            RedisDsn | str | None,
            Doc(
                """
                The Redis URL.

                If not provided, the URL will be taken from the
                environment variables `REDIS_URL` or `REDIS_HOST`,
                `REDIS_PORT`, `REDIS_DB`, and `REDIS_PASSWORD`.
                """
            ),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix prepended to every Redis key the backend
                writes. Use it to avoid collisions with other
                consumers of the same Redis database.

                By default no prefix is added.
                """
            ),
        ] = "",
        auto_register: Annotated[
            bool,
            Doc(
                """
                Automatically register the backend as the default
                for rate limiters.

                Set to `False` to manage multiple backends manually.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter backend."""
        self._url, self._redis = _create_redis_client(
            url, ResilienceSettingsValidationError
        )
        self._prefix = prefix
        self._auto_registered = auto_register
        if auto_register:
            rate_limiter_backend_registry.set(self)

    async def __aenter__(self) -> Self:
        """Open the rate limiter backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the rate limiter backend."""
        await self._redis.aclose()
        if (
            self._auto_registered
            and rate_limiter_backend_registry.is_loaded
            and rate_limiter_backend_registry.get() is self
        ):
            rate_limiter_backend_registry.reset()

    def bind(self, config: RateLimiterConfig) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Each strategy has its own Lua scripts. It registers them
        with the Redis client when the strategy is created.
        """
        match config:
            case TokenBucketConfig():
                return _RedisTokenBucket(self._redis, self._prefix, config)
            case GCRAConfig():
                return _RedisGCRA(self._redis, self._prefix, config)
        assert_never(config)


class _RedisGCRA(RateLimiterStrategy):
    """Redis GCRA strategy. Private.

    Prepends a per-algorithm discriminator to every Redis key so
    that a GCRA limiter and a token-bucket limiter sharing the same
    name cannot collide (they would hit each other's stored values
    with mismatched Redis types otherwise).
    """

    _ALGO_PREFIX = "gcra:"

    _LUA_ACQUIRE = """
        local key = KEYS[1]
        local burst = tonumber(ARGV[1])
        local rate = tonumber(ARGV[2])
        local period = tonumber(ARGV[3])
        local cost = tonumber(ARGV[4])

        local emission_interval = period / rate
        local increment = emission_interval * cost
        local burst_offset = emission_interval * burst

        -- Use Redis server time for cross-process consistency
        local now = redis.call("TIME")
        -- Offset to Jan 1 2017 to avoid double-precision issues
        local jan_1_2017 = 1483228800
        now = (now[1] - jan_1_2017) + (now[2] / 1000000)

        local tat = redis.call("GET", key)
        if not tat then
            tat = now
        else
            tat = tonumber(tat)
        end

        local new_tat = math.max(tat, now) + increment
        local allow_at = new_tat - burst_offset
        local diff = now - allow_at
        local remaining = math.floor(diff / emission_interval + 0.5)

        if remaining < 0 then
            local reset_after = tat - now
            local retry_after = diff * -1
            return {0, 0, tostring(retry_after), tostring(reset_after)}
        end

        local reset_after = new_tat - now
        redis.call("SET", key, new_tat, "EX", math.max(1, math.ceil(reset_after)))
        return {1, remaining, "0", tostring(reset_after)}
    """

    _LUA_PEEK = """
        local key = KEYS[1]
        local rate = tonumber(ARGV[1])
        local period = tonumber(ARGV[2])

        local emission_interval = period / rate

        local now = redis.call("TIME")
        local jan_1_2017 = 1483228800
        now = (now[1] - jan_1_2017) + (now[2] / 1000000)

        local tat = redis.call("GET", key)
        if not tat then
            tat = now
        else
            tat = tonumber(tat)
        end

        local new_tat = math.max(tat, now)
        local allow_at = new_tat - period
        local diff = now - allow_at
        local remaining = math.floor(diff / emission_interval + 0.5)

        -- Use <= 0 (not < 0 like acquire): remaining=0 means the next
        -- acquire(cost=1) would be rejected, so peek reports allowed=false.
        if remaining <= 0 then
            local reset_after = math.max(0, tat - now)
            local retry_after = emission_interval - diff
            if remaining < 0 then
                retry_after = diff * -1
            end
            return {0, 0, tostring(math.max(0, retry_after)), tostring(reset_after)}
        end

        local reset_after = new_tat - now
        return {1, remaining, "0", tostring(reset_after)}
    """

    def __init__(
        self,
        redis: Redis,
        prefix: str,
        config: GCRAConfig,
    ) -> None:
        self._redis = redis
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._lua_acquire = redis.register_script(self._LUA_ACQUIRE)
        self._lua_peek = redis.register_script(self._LUA_PEEK)
        self._limit = config.limit
        self._window = config.window

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (GCRA)."""
        result: list[Any] = await self._lua_acquire(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._limit, self._limit, self._window, cost],
            client=self._redis,
        )
        return RateLimitResult(
            allowed=bool(result[0]),
            limit=self._limit,
            remaining=int(result[1]),
            retry_after=float(result[2]),
            reset_after=float(result[3]),
        )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (GCRA)."""
        result: list[Any] = await self._lua_peek(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._limit, self._window],
            client=self._redis,
        )
        return RateLimitResult(
            allowed=bool(result[0]),
            limit=self._limit,
            remaining=int(result[1]),
            retry_after=float(result[2]),
            reset_after=float(result[3]),
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (GCRA)."""
        await self._redis.delete(f"{self._key_prefix}{key}")


class _RedisTokenBucket(RateLimiterStrategy):
    """Redis token-bucket strategy. Private.

    Continuous refill by `refill_rate` (tokens/sec), server-side
    `TIME` for cross-process clock consistency, and a
    `RateLimitResult`-shaped return payload so that both algorithms
    expose a uniform Python surface.

    Prepends a per-algorithm discriminator to every Redis key so
    that a token-bucket limiter and a GCRA limiter sharing the same
    name cannot collide on Redis value types.
    """

    _ALGO_PREFIX = "tb:"

    # Lua scripts below adapt the HMGET/HSET hash-storage pattern
    # from an upstream project; see THIRD_PARTY_NOTICES.md.
    _LUA_ACQUIRE = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local refill_rate = tonumber(ARGV[2])
        local cost = tonumber(ARGV[3])

        -- Use Redis server time for cross-process consistency.
        local now_pair = redis.call("TIME")
        -- Offset to Jan 1 2017 to avoid double-precision issues.
        local jan_1_2017 = 1483228800
        local now = (now_pair[1] - jan_1_2017) + (now_pair[2] / 1000000)

        local stored = redis.call("HMGET", key, "tokens", "last")
        local tokens, last
        if stored[1] == false then
            tokens = capacity
            last = now
        else
            tokens = tonumber(stored[1])
            last = tonumber(stored[2])
        end

        -- Continuous refill: tokens gained = elapsed_seconds * rate.
        tokens = math.min(capacity, tokens + (now - last) * refill_rate)

        if tokens >= cost then
            local remaining = tokens - cost
            local reset_after = (capacity - remaining) / refill_rate
            redis.call("HSET", key, "tokens", remaining, "last", now)
            redis.call("EXPIRE", key, math.max(1, math.ceil(reset_after)))
            return {1, math.floor(remaining), "0", tostring(reset_after)}
        end

        local retry_after = (cost - tokens) / refill_rate
        local reset_after = (capacity - tokens) / refill_rate
        redis.call("HSET", key, "tokens", tokens, "last", now)
        redis.call("EXPIRE", key, math.max(1, math.ceil(reset_after)))
        return {
            0,
            math.floor(tokens),
            tostring(retry_after),
            tostring(reset_after),
        }
    """

    _LUA_PEEK = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local refill_rate = tonumber(ARGV[2])

        local now_pair = redis.call("TIME")
        local jan_1_2017 = 1483228800
        local now = (now_pair[1] - jan_1_2017) + (now_pair[2] / 1000000)

        local stored = redis.call("HMGET", key, "tokens", "last")
        local tokens, last
        if stored[1] == false then
            tokens = capacity
            last = now
        else
            tokens = tonumber(stored[1])
            last = tonumber(stored[2])
        end

        tokens = math.min(capacity, tokens + (now - last) * refill_rate)

        if tokens >= 1 then
            local reset_after = (capacity - tokens) / refill_rate
            return {1, math.floor(tokens), "0", tostring(reset_after)}
        end

        local retry_after = (1 - tokens) / refill_rate
        local reset_after = (capacity - tokens) / refill_rate
        return {
            0,
            math.floor(tokens),
            tostring(retry_after),
            tostring(reset_after),
        }
    """

    def __init__(
        self,
        redis: Redis,
        prefix: str,
        config: TokenBucketConfig,
    ) -> None:
        self._redis = redis
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._lua_acquire = redis.register_script(self._LUA_ACQUIRE)
        self._lua_peek = redis.register_script(self._LUA_PEEK)
        self._capacity = config.capacity
        self._refill_rate = config.refill_rate

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (token bucket)."""
        result: list[Any] = await self._lua_acquire(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._capacity, self._refill_rate, cost],
            client=self._redis,
        )
        return RateLimitResult(
            allowed=bool(result[0]),
            limit=int(self._capacity),
            remaining=int(result[1]),
            retry_after=float(result[2]),
            reset_after=float(result[3]),
        )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (token bucket)."""
        result: list[Any] = await self._lua_peek(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._capacity, self._refill_rate],
            client=self._redis,
        )
        return RateLimitResult(
            allowed=bool(result[0]),
            limit=int(self._capacity),
            remaining=int(result[1]),
            retry_after=float(result[2]),
            reset_after=float(result[3]),
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (token bucket)."""
        await self._redis.delete(f"{self._key_prefix}{key}")
