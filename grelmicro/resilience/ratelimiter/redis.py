"""Redis rate-limiter adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
    unsupported_algorithm,
)
from grelmicro.resilience.ratelimiter import _gcra
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

if TYPE_CHECKING:
    from types import TracebackType

    from redis.asyncio import Redis

    from grelmicro.types import BackendScope


class RedisRateLimiterAdapter(RateLimiterBackend):
    """Redis rate limiter adapter.

    Wraps a `RedisProvider` and supports both
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    and [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]
    algorithm configs via atomic Lua scripts. Safe across processes
    and machines.

    Example:
    ```python
    from grelmicro.providers.redis import RedisProvider
    from grelmicro.resilience import RateLimiter
    from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter


    async def main() -> None:
        provider = RedisProvider("redis://localhost:6379/0")
        async with RedisRateLimiterAdapter(provider=provider):
            rl = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)
            await rl.acquire(key="u1")
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

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
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `RedisProvider` when `provider` is not set. Defaults
                to `REDIS_`. Use a custom prefix to split pools.
                """
            ),
        ] = "REDIS_",
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix prepended to every Redis key the adapter
                writes. Use it to avoid collisions with other
                consumers of the same Redis database.
                """
            ),
        ] = "",
    ) -> None:
        """Initialize the rate limiter adapter."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._prefix = prefix

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the rate limiter adapter."""
        if self._owns_provider:
            await self._provider.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the rate limiter adapter."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    def bind(
        self,
        config: TokenBucketConfig | SlidingWindowConfig,
    ) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Each strategy has its own Lua scripts. It registers them
        with the Redis client when the strategy is created.
        """
        match config:
            case TokenBucketConfig():
                return _RedisTokenBucket(
                    self._provider.client, self._prefix, config
                )
            case SlidingWindowConfig():
                return _RedisGCRA(self._provider.client, self._prefix, config)
        raise unsupported_algorithm(config)


class _RedisGCRA(RateLimiterStrategy):
    """Redis GCRA strategy. Private.

    Prepends a per-algorithm discriminator to every Redis key so
    that a GCRA limiter and a token-bucket limiter sharing the same
    name cannot collide (they would hit each other's stored values
    with mismatched Redis types otherwise).
    """

    _ALGO_PREFIX = "gcra_us:"

    _LUA_ACQUIRE = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local emission = tonumber(ARGV[2])
        local cost = tonumber(ARGV[3])

        -- Redis server time, in whole microseconds, for cross-process consistency
        local time = redis.call("TIME")
        local now = tonumber(time[1]) * 1000000 + tonumber(time[2])

        local tat = tonumber(redis.call("GET", key)) or now
        local gap = math.max(0, tat - now)
        local reset = gap + emission * cost
        local diff = emission * limit - reset

        if diff < 0 then
            return {0, 0, -diff, gap}
        end

        -- A number argument is written with 14 digits, too few for the time.
        redis.call(
            "SET", key, string.format("%.0f", now + reset),
            "PX", math.max(1, math.ceil(reset / 1000))
        )
        return {1, math.floor(diff / emission), 0, reset}
    """

    _LUA_PEEK = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local emission = tonumber(ARGV[2])

        local time = redis.call("TIME")
        local now = tonumber(time[1]) * 1000000 + tonumber(time[2])

        local tat = tonumber(redis.call("GET", key)) or now
        local gap = math.max(0, tat - now)
        local diff = emission * limit - gap
        local remaining = math.floor(diff / emission)

        -- remaining=0 means the next acquire(cost=1) would be refused.
        if remaining <= 0 then
            return {0, 0, emission - diff, gap}
        end
        return {1, remaining, 0, gap}
    """

    def __init__(
        self,
        redis: Redis,
        prefix: str,
        config: SlidingWindowConfig,
    ) -> None:
        self._redis = redis
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._lua_acquire = redis.register_script(self._LUA_ACQUIRE)
        self._lua_peek = redis.register_script(self._LUA_PEEK)
        self._limit = config.limit
        self._emission = _gcra.emission_interval(config.window, config.limit)

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (GCRA)."""
        result: list[Any] = await self._lua_acquire(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._limit, self._emission, cost],
            client=self._redis,
        )
        return self._result(result)

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (GCRA)."""
        result: list[Any] = await self._lua_peek(
            keys=[f"{self._key_prefix}{key}"],
            args=[self._limit, self._emission],
            client=self._redis,
        )
        return self._result(result)

    def _result(self, reply: list[Any]) -> RateLimitResult:
        """Return the result a script replied, its times in microseconds."""
        return RateLimitResult(
            allowed=bool(reply[0]),
            limit=self._limit,
            remaining=int(reply[1]),
            retry_after=int(reply[2]) / _gcra.MICROSECONDS,
            reset_after=int(reply[3]) / _gcra.MICROSECONDS,
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
