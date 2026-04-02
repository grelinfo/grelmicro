"""Redis Rate Limiter Backend."""

from types import TracebackType
from typing import Annotated, Self

from pydantic import RedisDsn
from typing_extensions import Doc

from grelmicro._redis import _create_redis_client
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import RateLimiterBackend, RateLimitResult
from grelmicro.resilience.errors import ResilienceSettingsValidationError


class RedisRateLimiterBackend(RateLimiterBackend):
    """Redis Rate Limiter Backend.

    Uses the GCRA (Generic Cell Rate Algorithm) for atomic
    sliding-window rate limiting. Stores a single Theoretical
    Arrival Time (TAT) per key (~72 bytes).
    """

    _LUA_GCRA = """
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

    def __init__(
        self,
        url: Annotated[
            RedisDsn | str | None,
            Doc("""
                The Redis URL.

                If not provided, the URL will be taken from the
                environment variables REDIS_URL or REDIS_HOST,
                REDIS_PORT, REDIS_DB, and REDIS_PASSWORD.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                The prefix to add on redis keys to avoid
                conflicts with other keys.

                By default no prefix is added.
                """),
        ] = "",
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register the rate limiter backend"
                " in the backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter backend."""
        self._url, self._redis = _create_redis_client(
            url, ResilienceSettingsValidationError
        )
        self._prefix = prefix
        self._lua_gcra = self._redis.register_script(self._LUA_GCRA)
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

    async def acquire(
        self,
        *,
        key: str,
        limit: int,
        window: float,
        cost: int,
    ) -> RateLimitResult:
        """Try to acquire rate limit tokens using GCRA.

        Args:
            key: The rate limit key.
            limit: Maximum requests allowed in the window.
            window: Window duration in seconds.
            cost: Number of tokens to consume.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.
        """
        # burst=limit so full-window burst is allowed (standard behaviour).
        # rate=limit matches the "limit requests per window" semantics.
        result = await self._lua_gcra(
            keys=[f"{self._prefix}{key}"],
            args=[limit, limit, window, cost],
            client=self._redis,
        )

        allowed = bool(result[0])
        remaining = int(result[1])
        retry_after = float(result[2])
        reset_after = float(result[3])

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            retry_after=retry_after,
            reset_after=reset_after,
        )
