"""Redis circuit-breaker adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
)
from grelmicro.resilience.circuitbreaker import CircuitBreakerState

if TYPE_CHECKING:
    from types import TracebackType

    from redis.asyncio import Redis

    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )


class RedisCircuitBreakerAdapter(CircuitBreakerBackend):
    """Redis circuit breaker adapter.

    Builds a per-breaker
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    that stores state in a Redis hash keyed `{prefix}cb:{name}`. All
    admission and counter updates run as atomic Lua scripts so
    concurrent replicas converge to the same state without
    coordination locks.

    Today the consecutive-count algorithm is the only strategy. Future
    algorithm configs plug in through the same `bind` entry point.

    `last_error` and `last_error_time` stay per-replica.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.redis import RedisProvider
    from grelmicro.resilience import CircuitBreakerRegistry, CircuitBreaker

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[redis, CircuitBreakerRegistry(redis)])
    payments = CircuitBreaker("payments")
    ```

    Read more in the [Circuit Breaker](../resilience/circuit-breaker.md) docs.
    """

    is_shared: ClassVar[bool] = True

    _KEY_PREFIX = "cb:"

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

    def bind(
        self,
        *,
        name: str,
        config: ConsecutiveCountConfig,
    ) -> CircuitBreakerStrategy:
        """Build a strategy for the named breaker and config.

        Dispatches on the `config.kind` discriminator. Today only
        `consecutive_count` is supported.
        """
        if config.kind == "consecutive_count":
            return _RedisConsecutiveCountStrategy(
                client=self._provider.client,
                key=f"{self._key_prefix}{name}",
                config=config,
            )
        msg = f"Unsupported circuit breaker algorithm: {config.kind!r}"
        raise NotImplementedError(msg)


class _RedisConsecutiveCountStrategy(CircuitBreakerStrategy):
    """Redis consecutive-count strategy.

    Stores state in a Redis hash:

    - `state` - one of `CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`,
      `FORCED_CLOSED`.
    - `opened_at` - Redis-server epoch seconds when the breaker
      entered OPEN. Absent or `0` otherwise.
    - `cool_down` - seconds the breaker should stay OPEN before
      transitioning to HALF_OPEN.
    - `cerr` - consecutive error count.
    - `csucc` - consecutive success count.
    - `ho_admit` - probes admitted while in HALF_OPEN. Reset on every
      state transition.
    """

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
            return {state, 0, 0, tostring(opened_at)}
        end

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

        if state == "HALF_OPEN" and csucc >= threshold then
            state = "CLOSED"
            redis.call(
                "HSET", key,
                "state", state, "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            redis.call("HDEL", key, "opened_at", "cool_down")
            return {state, 0, 0, "0"}
        end

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
        else
            redis.call(
                "HSET", key,
                "state", desired, "cerr", 0, "csucc", 0, "ho_admit", 0
            )
            redis.call("HDEL", key, "opened_at", "cool_down")
        end
        redis.call("PERSIST", key)
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
        client: Redis,
        key: str,
        config: ConsecutiveCountConfig,
    ) -> None:
        """Bind the strategy to the breaker's key and config."""
        self._client = client
        self._key = key
        self._error_threshold = config.error_threshold
        self._success_threshold = config.success_threshold
        self._reset_timeout = config.reset_timeout
        self._half_open_capacity = config.half_open_capacity
        self._lua_try_acquire = client.register_script(self._LUA_TRY_ACQUIRE)
        self._lua_record_error = client.register_script(self._LUA_RECORD_ERROR)
        self._lua_record_success = client.register_script(
            self._LUA_RECORD_SUCCESS
        )
        self._lua_transition = client.register_script(self._LUA_TRANSITION)
        self._lua_get_state = client.register_script(self._LUA_GET_STATE)

    async def try_acquire(self) -> bool:
        """Atomic admission via Lua."""
        result = await self._lua_try_acquire(
            keys=[self._key],
            args=[self._half_open_capacity, self._reset_timeout],
            client=self._client,
        )
        return bool(result)

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,  # noqa: ARG002
    ) -> CircuitBreakerSnapshot:
        """Atomic outcome record with conditional state transition."""
        if success:
            result: list[Any] = await self._lua_record_success(
                keys=[self._key],
                args=[self._success_threshold, self._reset_timeout],
                client=self._client,
            )
        else:
            result = await self._lua_record_error(
                keys=[self._key],
                args=[self._error_threshold, self._reset_timeout],
                client=self._client,
            )
        return self._unpack(result)

    async def transition(
        self,
        *,
        desired: CircuitBreakerState,
        cool_down: float | None = None,
    ) -> None:
        """Manual transition. Last-write-wins."""
        await self._lua_transition(
            keys=[self._key],
            args=[
                desired.value,
                cool_down if cool_down is not None else self._reset_timeout,
            ],
            client=self._client,
        )

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current snapshot."""
        result: list[Any] = await self._lua_get_state(
            keys=[self._key],
            client=self._client,
        )
        return self._unpack(result)

    @staticmethod
    def _unpack(result: list[Any]) -> CircuitBreakerSnapshot:
        state_raw = result[0]
        if isinstance(state_raw, bytes):  # pragma: no branch
            state_raw = state_raw.decode()
        return CircuitBreakerSnapshot(
            state=CircuitBreakerState(state_raw),
            opened_at=float(result[3]),
            consecutive_error_count=int(result[1]),
            consecutive_success_count=int(result[2]),
        )
