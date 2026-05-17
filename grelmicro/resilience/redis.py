"""Redis Resilience Adapters."""

import asyncio
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self, assert_never

from redis.asyncio import Redis
from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSharedState,
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import (
    RateLimiterConfig,
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.circuitbreaker import CircuitBreakerState

if TYPE_CHECKING:
    from grelmicro.resilience.circuitbreaker import CircuitBreaker


class RedisRateLimiterAdapter(RateLimiterBackend):
    """Redis rate limiter adapter.

    Wraps a `RedisProvider` and supports both
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    and [`SlidingWindowConfig`][grelmicro.resilience.algorithms.SlidingWindowConfig]
    algorithm configs via atomic Lua scripts. Safe across processes
    and machines.

    Example:
    ```python
    from grelmicro.providers.redis import RedisProvider
    from grelmicro.resilience import RateLimiter, TokenBucketConfig
    from grelmicro.resilience.redis import RedisRateLimiterAdapter


    async def main() -> None:
        provider = RedisProvider("redis://localhost:6379/0")
        async with RedisRateLimiterAdapter(provider=provider):
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

    def bind(self, config: RateLimiterConfig) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Each strategy has its own Lua scripts. It registers them
        with the Redis client when the strategy is created.
        """
        client = self._provider.client
        match config:
            case TokenBucketConfig():
                return _RedisTokenBucket(client, self._prefix, config)
            case SlidingWindowConfig():
                return _RedisGCRA(client, self._prefix, config)
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
        config: SlidingWindowConfig,
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


class RedisCircuitBreakerAdapter(CircuitBreakerBackend):
    """Redis circuit breaker adapter.

    Stores breaker state in a Redis hash per name, keyed
    `{prefix}cb:{name}`. All admission and counter updates run as
    atomic Lua scripts so concurrent replicas converge to the same
    state without coordination locks.

    Fields stored per hash:

    - `state` - one of `CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`,
      `FORCED_CLOSED`.
    - `opened_at` - Redis-server epoch seconds when the breaker
      entered OPEN. Absent or `0` otherwise.
    - `cerr` - consecutive error count.
    - `csucc` - consecutive success count.
    - `ho_admit` - probes admitted while in HALF_OPEN. Reset on every
      state transition.

    `last_error` and `last_error_time` stay per-replica.

    Example:
    ```python
    from grelmicro.providers.redis import RedisProvider
    from grelmicro.resilience import Breaker, CircuitBreaker
    from grelmicro import Grelmicro

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[redis, Breaker(redis)])
    payments = CircuitBreaker("payments")
    ```

    Read more in the [Circuit Breaker](../resilience/circuit-breaker.md) docs.
    """

    is_shared: ClassVar[bool] = True

    _KEY_PREFIX = "cb:"

    _STATE_CLOSED = "CLOSED"
    _STATE_OPEN = "OPEN"
    _STATE_HALF_OPEN = "HALF_OPEN"
    _STATE_FORCED_OPEN = "FORCED_OPEN"
    _STATE_FORCED_CLOSED = "FORCED_CLOSED"

    _LUA_TRY_ACQUIRE = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local reset_timeout = tonumber(ARGV[2])

        local now_pair = redis.call("TIME")
        local now = now_pair[1] + (now_pair[2] / 1000000)

        local stored = redis.call(
            "HMGET", key, "state", "opened_at", "ho_admit", "cool_down"
        )
        local state = stored[1]
        if state == false then state = "CLOSED" end
        local opened_at = tonumber(stored[2]) or 0
        local ho_admit = tonumber(stored[3]) or 0
        local cool_down = tonumber(stored[4]) or reset_timeout

        if state == "FORCED_CLOSED" or state == "CLOSED" then
            return 1
        end

        if state == "FORCED_OPEN" then
            return 0
        end

        if state == "OPEN" then
            if now >= opened_at + cool_down then
                state = "HALF_OPEN"
                ho_admit = 0
                redis.call(
                    "HSET", key,
                    "state", state, "cerr", 0, "csucc", 0, "ho_admit", 0
                )
                redis.call("HDEL", key, "opened_at", "cool_down")
            else
                return 0
            end
        end

        if state == "HALF_OPEN" then
            if ho_admit < capacity then
                redis.call("HINCRBY", key, "ho_admit", 1)
                local ttl = math.max(1, math.ceil(reset_timeout * 2))
                redis.call("EXPIRE", key, ttl)
                return 1
            end
            return 0
        end

        return 0
    """

    _LUA_RECORD_ERROR = """
        local key = KEYS[1]
        local threshold = tonumber(ARGV[1])
        local reset_timeout = tonumber(ARGV[2])

        local stored = redis.call("HMGET", key, "state", "opened_at")
        local state = stored[1]
        if state == false then state = "CLOSED" end
        local opened_at = tonumber(stored[2]) or 0

        if state == "FORCED_OPEN" or state == "FORCED_CLOSED" or state == "OPEN" then
            return {state, 0, 0, tostring(opened_at)}
        end

        local cerr = redis.call("HINCRBY", key, "cerr", 1)
        redis.call("HSET", key, "csucc", 0)

        if state == "HALF_OPEN" then
            local ho_admit = tonumber(redis.call("HGET", key, "ho_admit")) or 0
            if ho_admit > 0 then
                redis.call("HINCRBY", key, "ho_admit", -1)
            end
        end

        local ttl = math.max(1, math.ceil(reset_timeout * 2))

        if cerr >= threshold then
            local now_pair = redis.call("TIME")
            local now = now_pair[1] + (now_pair[2] / 1000000)
            state = "OPEN"
            opened_at = now
            redis.call(
                "HSET", key,
                "state", state, "opened_at", opened_at,
                "cool_down", reset_timeout,
                "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            redis.call("EXPIRE", key, ttl)
            return {state, 0, 0, tostring(opened_at)}
        end

        redis.call("EXPIRE", key, ttl)
        return {state, cerr, 0, tostring(opened_at)}
    """

    _LUA_RECORD_SUCCESS = """
        local key = KEYS[1]
        local threshold = tonumber(ARGV[1])
        local reset_timeout = tonumber(ARGV[2])

        local stored = redis.call("HMGET", key, "state", "opened_at")
        local state = stored[1]
        if state == false then state = "CLOSED" end
        local opened_at = tonumber(stored[2]) or 0

        if state == "FORCED_OPEN" or state == "FORCED_CLOSED" or state == "OPEN" then
            return {state, 0, 0, tostring(opened_at)}
        end

        local csucc = redis.call("HINCRBY", key, "csucc", 1)
        redis.call("HSET", key, "cerr", 0)

        if state == "HALF_OPEN" then
            local ho_admit = tonumber(redis.call("HGET", key, "ho_admit")) or 0
            if ho_admit > 0 then
                redis.call("HINCRBY", key, "ho_admit", -1)
            end
        end

        local ttl = math.max(1, math.ceil(reset_timeout * 2))

        if state == "HALF_OPEN" and csucc >= threshold then
            state = "CLOSED"
            redis.call(
                "HSET", key,
                "state", state, "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            redis.call("HDEL", key, "opened_at", "cool_down")
            return {state, 0, 0, "0"}
        end

        redis.call("EXPIRE", key, ttl)
        return {state, 0, csucc, tostring(opened_at)}
    """

    _LUA_TRANSITION = """
        local key = KEYS[1]
        local desired = ARGV[1]
        local cool_down = tonumber(ARGV[2])

        if desired == "OPEN" then
            local now_pair = redis.call("TIME")
            local now = now_pair[1] + (now_pair[2] / 1000000)
            redis.call(
                "HSET", key,
                "state", desired, "opened_at", now, "cool_down", cool_down,
                "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            if cool_down > 0 then
                local ttl = math.max(1, math.ceil(cool_down * 2))
                redis.call("EXPIRE", key, ttl)
            end
        else
            redis.call(
                "HSET", key,
                "state", desired, "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            redis.call("HDEL", key, "opened_at", "cool_down")
        end
    """

    _LUA_GET_STATE = """
        local key = KEYS[1]
        local stored = redis.call("HMGET", key, "state", "cerr", "csucc", "opened_at")
        local state = stored[1]
        if state == false then state = "CLOSED" end
        local cerr = tonumber(stored[2]) or 0
        local csucc = tonumber(stored[3]) or 0
        local opened_at = tonumber(stored[4]) or 0
        return {state, cerr, csucc, tostring(opened_at)}
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
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `RedisProvider` when `provider` is not set. Defaults
                to `REDIS_`.
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
        """Initialize the circuit breaker adapter."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._prefix = prefix
        self._key_prefix = f"{prefix}{self._KEY_PREFIX}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lua_try_acquire: Any = None
        self._lua_record_error: Any = None
        self._lua_record_success: Any = None
        self._lua_transition: Any = None
        self._lua_get_state: Any = None

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the circuit breaker adapter."""
        if self._owns_provider:
            await self._provider.__aenter__()
        client = self._provider.client
        self._lua_try_acquire = client.register_script(self._LUA_TRY_ACQUIRE)
        self._lua_record_error = client.register_script(self._LUA_RECORD_ERROR)
        self._lua_record_success = client.register_script(
            self._LUA_RECORD_SUCCESS
        )
        self._lua_transition = client.register_script(self._LUA_TRANSITION)
        self._lua_get_state = client.register_script(self._LUA_GET_STATE)
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the circuit breaker adapter."""
        self._loop = None
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    def register(self, breaker: "CircuitBreaker") -> None:
        """Bind a breaker to the adapter (no per-breaker local state)."""

    async def try_acquire(
        self,
        *,
        name: str,
        half_open_capacity: int,
        reset_timeout: float,
    ) -> bool:
        """Atomic admission via Lua."""
        result = await self._lua_try_acquire(
            keys=[self._key(name)],
            args=[half_open_capacity, reset_timeout],
            client=self._provider.client,
        )
        return bool(result)

    async def record_error(
        self,
        *,
        name: str,
        error_threshold: int,
        reset_timeout: float,
    ) -> CircuitBreakerSharedState:
        """Atomic error record with conditional OPEN transition."""
        result: list[Any] = await self._lua_record_error(
            keys=[self._key(name)],
            args=[error_threshold, reset_timeout],
            client=self._provider.client,
        )
        return self._unpack(result)

    async def record_success(
        self,
        *,
        name: str,
        success_threshold: int,
        reset_timeout: float,
    ) -> CircuitBreakerSharedState:
        """Atomic success record with conditional CLOSED transition."""
        result: list[Any] = await self._lua_record_success(
            keys=[self._key(name)],
            args=[success_threshold, reset_timeout],
            client=self._provider.client,
        )
        return self._unpack(result)

    async def transition(
        self,
        *,
        name: str,
        desired: CircuitBreakerState,
        reset_timeout: float | None = None,
    ) -> None:
        """Manual transition. Last-write-wins."""
        await self._lua_transition(
            keys=[self._key(name)],
            args=[
                desired.value,
                reset_timeout if reset_timeout is not None else 0,
            ],
            client=self._provider.client,
        )

    async def get_state(self, *, name: str) -> CircuitBreakerSharedState:
        """Read the current shared state."""
        result: list[Any] = await self._lua_get_state(
            keys=[self._key(name)],
            client=self._provider.client,
        )
        return self._unpack(result)

    def _key(self, name: str) -> str:
        return f"{self._key_prefix}{name}"

    @staticmethod
    def _unpack(result: list[Any]) -> CircuitBreakerSharedState:
        state_raw = result[0]
        if isinstance(state_raw, bytes):
            state_raw = state_raw.decode()
        return CircuitBreakerSharedState(
            state=CircuitBreakerState(state_raw),
            consecutive_error_count=int(result[1]),
            consecutive_success_count=int(result[2]),
            opened_at=float(result[3]),
        )
