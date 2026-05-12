"""Memory Resilience Backends and standalone primitives."""

import asyncio
import math
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Self, assert_never
from weakref import WeakSet

from typing_extensions import Doc

from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import (
    GCRAConfig,
    RateLimiterConfig,
    TokenBucketConfig,
)

if TYPE_CHECKING:
    from grelmicro.resilience.circuitbreaker import CircuitBreaker

_EVICTION_THRESHOLD = 1000


class MemoryTokenBucket:
    """Standalone in-memory token bucket.

    Public, **synchronous**, thread-safe, and keyed. Use this
    class directly when you need fast, in-process, burst-friendly
    rate limiting in synchronous code. A typical use is inside a
    `logging.Filter`.

    For async workflows, distributed coordination, or alternative
    algorithms, use
    [`RateLimiter`][grelmicro.resilience.RateLimiter] with a
    backend instead.

    Example:
    ```python
    from grelmicro.resilience.memory import MemoryTokenBucket

    bucket = MemoryTokenBucket(capacity=5, refill_rate=1)


    def handle(key: str) -> bool:
        return bucket.try_acquire(key=key)
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(
        self,
        *,
        capacity: Annotated[
            int,
            Doc(
                "Maximum burst size. The bucket never holds more "
                "than `capacity` tokens."
            ),
        ],
        refill_rate: Annotated[
            float,
            Doc("Tokens replenished per second, up to `capacity`."),
        ],
    ) -> None:
        """Initialize the token bucket."""
        if capacity <= 0:
            msg = f"capacity must be greater than 0, got {capacity}"
            raise ValueError(msg)
        if refill_rate <= 0:
            msg = f"refill_rate must be greater than 0, got {refill_rate}"
            raise ValueError(msg)
        self._capacity_int = capacity
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate)
        # Per-key state: (tokens, last_refill_monotonic)
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = Lock()

    @property
    def capacity(self) -> int:
        """Configured bucket capacity."""
        return self._capacity_int

    @property
    def refill_rate(self) -> float:
        """Configured refill rate (tokens per second)."""
        return self._refill_rate

    def _refill(self, tokens: float, last: float, now: float) -> float:
        return min(self._capacity, tokens + (now - last) * self._refill_rate)

    def _maybe_evict(self, now: float) -> None:
        if len(self._state) <= _EVICTION_THRESHOLD:
            return
        # Evict keys that have refilled to full capacity.
        to_remove = [
            k
            for k, (t, last) in self._state.items()
            if self._refill(t, last, now) >= self._capacity
        ]
        for k in to_remove:
            del self._state[k]

    def try_acquire(
        self,
        key: Annotated[
            str,
            Doc("Identifier of the bucket (e.g. logger name, user id)."),
        ] = "",
        *,
        cost: Annotated[
            float,
            Doc("Tokens to consume. Must be > 0 and <= `capacity`."),
        ] = 1.0,
    ) -> bool:
        """Try to consume `cost` tokens for `key`.

        Returns `True` and deducts the cost when the bucket has
        enough tokens, otherwise `False` (nothing deducted).
        """
        if cost <= 0 or cost > self._capacity:
            msg = f"cost must be in (0, {self._capacity}], got {cost}"
            raise ValueError(msg)
        now = monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self._capacity, now))
            tokens = self._refill(tokens, last, now)
            if tokens >= cost:
                self._state[key] = (tokens - cost, now)
                self._maybe_evict(now)
                return True
            self._state[key] = (tokens, now)
            self._maybe_evict(now)
            return False

    def peek(
        self,
        key: Annotated[
            str,
            Doc("Identifier of the bucket."),
        ] = "",
    ) -> float:
        """Return the current token count without consuming any."""
        now = monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self._capacity, now))
            return self._refill(tokens, last, now)

    def reset(
        self,
        key: Annotated[
            str,
            Doc("Identifier of the bucket to reset."),
        ] = "",
    ) -> None:
        """Delete state for `key`, restoring full capacity."""
        with self._lock:
            self._state.pop(key, None)


class MemoryRateLimiterAdapter(RateLimiterBackend):
    """In-memory rate limiter backend.

    Supports both
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    and [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig]
    algorithm configs. State is held in separate per-algorithm
    maps so two rate limiters with the same name but different
    algorithms cannot collide. Thread-safe.

    Use it for tests and single-process deployments. For
    distributed coordination, use
    [`RedisRateLimiterAdapter`][grelmicro.resilience.redis.RedisRateLimiterAdapter].

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucketConfig, use_backend
    from grelmicro.resilience.memory import MemoryRateLimiterAdapter

    use_backend(MemoryRateLimiterAdapter())
    rl = RateLimiter("api", TokenBucketConfig(capacity=10, refill_rate=1))
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(self) -> None:
        """Initialize the rate limiter backend."""
        # Separate per-algorithm state maps so keys never alias
        # across algorithms.
        self._token_bucket_state: dict[str, tuple[float, float]] = {}
        self._gcra_state: dict[str, float] = {}
        self._lock = Lock()

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
        with self._lock:
            self._token_bucket_state.clear()
            self._gcra_state.clear()

    def bind(self, config: RateLimiterConfig) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Called once by
        [`RateLimiter`][grelmicro.resilience.RateLimiter] when
        the rate limiter is created. This is the only place that
        picks which algorithm to run. Later calls to `acquire`,
        `peek`, and `reset` use the returned strategy directly.
        """
        match config:
            case TokenBucketConfig():
                return _MemoryTokenBucket(
                    self._token_bucket_state, self._lock, config
                )
            case GCRAConfig():
                return _MemoryGCRA(self._gcra_state, self._lock, config)
        assert_never(config)


class _MemoryTokenBucket(RateLimiterStrategy):
    """In-memory token-bucket strategy. Private."""

    def __init__(
        self,
        state: dict[str, tuple[float, float]],
        lock: Lock,
        config: TokenBucketConfig,
    ) -> None:
        self._state = state
        self._lock = lock
        self._capacity = config.capacity
        self._refill_rate = config.refill_rate

    def _refill(self, tokens: float, last: float, now: float) -> float:
        return min(self._capacity, tokens + (now - last) * self._refill_rate)

    def _maybe_evict(self, now: float) -> None:
        if len(self._state) <= _EVICTION_THRESHOLD:
            return
        # Evict keys that have refilled to full capacity: they
        # behave identically to fresh keys.
        to_remove = [
            k
            for k, (tokens, last) in self._state.items()
            if self._refill(tokens, last, now) >= self._capacity
        ]
        for k in to_remove:
            del self._state[k]

    async def acquire(
        self,
        *,
        key: str,
        cost: int,
    ) -> RateLimitResult:
        """Async acquire (token bucket)."""
        now = monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self._capacity, now))
            tokens = self._refill(tokens, last, now)
            if tokens >= cost:
                remaining = tokens - cost
                self._state[key] = (remaining, now)
                self._maybe_evict(now)
                return RateLimitResult(
                    allowed=True,
                    limit=int(self._capacity),
                    remaining=int(remaining),
                    retry_after=0.0,
                    reset_after=(self._capacity - remaining)
                    / self._refill_rate,
                )
            self._state[key] = (tokens, now)
            self._maybe_evict(now)
            return RateLimitResult(
                allowed=False,
                limit=int(self._capacity),
                remaining=int(tokens),
                retry_after=(cost - tokens) / self._refill_rate,
                reset_after=(self._capacity - tokens) / self._refill_rate,
            )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (token bucket)."""
        now = monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self._capacity, now))
            tokens = self._refill(tokens, last, now)
            if tokens >= 1.0:
                return RateLimitResult(
                    allowed=True,
                    limit=int(self._capacity),
                    remaining=int(tokens),
                    retry_after=0.0,
                    reset_after=(self._capacity - tokens) / self._refill_rate,
                )
            return RateLimitResult(
                allowed=False,
                limit=int(self._capacity),
                remaining=int(tokens),
                retry_after=(1.0 - tokens) / self._refill_rate,
                reset_after=(self._capacity - tokens) / self._refill_rate,
            )

    async def reset(self, *, key: str) -> None:
        """Async reset (token bucket)."""
        with self._lock:
            self._state.pop(key, None)


class _MemoryGCRA(RateLimiterStrategy):
    """In-memory GCRA strategy. Private."""

    def __init__(
        self,
        state: dict[str, float],
        lock: Lock,
        config: GCRAConfig,
    ) -> None:
        self._state = state
        self._lock = lock
        self._limit = config.limit
        self._window = config.window
        self._emission_interval = config.window / config.limit

    def _maybe_evict(self, now: float) -> None:
        if len(self._state) <= _EVICTION_THRESHOLD:
            return
        to_remove = [k for k, tat in self._state.items() if tat < now]
        for k in to_remove:
            del self._state[k]

    async def acquire(
        self,
        *,
        key: str,
        cost: int,
    ) -> RateLimitResult:
        """Async acquire (GCRA)."""
        now = monotonic()
        increment = self._emission_interval * cost
        with self._lock:
            self._maybe_evict(now)
            tat = self._state.get(key, now)

            new_tat = max(tat, now) + increment
            allow_at = new_tat - self._window
            diff = now - allow_at
            remaining = math.floor(diff / self._emission_interval + 0.5)

            if remaining < 0:
                reset_after = tat - now
                retry_after = -diff
                return RateLimitResult(
                    allowed=False,
                    limit=self._limit,
                    remaining=0,
                    retry_after=max(0.0, retry_after),
                    reset_after=max(0.0, reset_after),
                )

            reset_after = new_tat - now
            self._state[key] = new_tat
            return RateLimitResult(
                allowed=True,
                limit=self._limit,
                remaining=remaining,
                retry_after=0.0,
                reset_after=reset_after,
            )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (GCRA)."""
        now = monotonic()
        with self._lock:
            tat = self._state.get(key, now)

            new_tat = max(tat, now)
            allow_at = new_tat - self._window
            diff = now - allow_at
            remaining = math.floor(diff / self._emission_interval + 0.5)

            if remaining <= 0:
                reset_after = tat - now
                retry_after = (
                    -diff if remaining < 0 else self._emission_interval - diff
                )
                return RateLimitResult(
                    allowed=False,
                    limit=self._limit,
                    remaining=0,
                    retry_after=max(0.0, retry_after),
                    reset_after=max(0.0, reset_after),
                )

            return RateLimitResult(
                allowed=True,
                limit=self._limit,
                remaining=remaining,
                retry_after=0.0,
                reset_after=max(0.0, new_tat - now),
            )

    async def reset(self, *, key: str) -> None:
        """Async reset (GCRA)."""
        with self._lock:
            self._state.pop(key, None)


class MemoryCircuitBreakerAdapter(CircuitBreakerBackend):
    """In-memory circuit breaker backend.

    State for every breaker bound to this backend is held in process.
    Closing the backend (typically through ``grelmicro.lifespan``)
    resets every registered breaker so the next start begins on a
    clean slate and any references the breaker still holds are
    released.

    Use it for tests and single-process deployments. A future
    Redis-backed implementation will share state across replicas
    (see issue #188).
    """

    def __init__(self) -> None:
        """Initialize the circuit breaker backend."""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._breakers: WeakSet[CircuitBreaker] = WeakSet()

    async def __aenter__(self) -> Self:
        """Open the backend and capture the running loop."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend, clearing every registered breaker."""
        for breaker in list(self._breakers):
            breaker._reset_state()  # noqa: SLF001
        self._breakers.clear()
        self._loop = None

    def register(self, breaker: "CircuitBreaker") -> None:
        """Bind a breaker so it is reset on close."""
        self._breakers.add(breaker)
