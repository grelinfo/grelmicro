"""Memory Rate Limiter Backend and standalone primitives."""

import math
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import Annotated, Self, assert_never

from typing_extensions import Doc

from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import GCRA, Algorithm, TokenBucket

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


class MemoryRateLimiterBackend(RateLimiterBackend):
    """In-memory rate limiter backend.

    Supports both
    [`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket] and
    [`GCRA`][grelmicro.resilience.algorithms.GCRA] algorithms.
    State is held in separate per-algorithm maps so two rate
    limiters with the same name but different algorithms cannot
    collide. Thread-safe.

    Use it for tests and single-process deployments. For
    distributed coordination, use
    [`RedisRateLimiterBackend`][grelmicro.resilience.redis.RedisRateLimiterBackend].

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucket
    from grelmicro.resilience.memory import MemoryRateLimiterBackend

    MemoryRateLimiterBackend()  # auto-registers
    rl = RateLimiter(
        "api", algorithm=TokenBucket(capacity=10, refill_rate=1)
    )
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(
        self,
        *,
        auto_register: Annotated[
            bool,
            Doc(
                """
                Automatically register the backend as the default
                for rate limiters.

                Set to `False` to manage the backend manually, for
                example when wiring multiple backends side-by-side
                in tests.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter backend."""
        # Separate per-algorithm state maps so keys never alias
        # across algorithms.
        self._token_bucket_state: dict[str, tuple[float, float]] = {}
        self._gcra_state: dict[str, float] = {}
        self._lock = Lock()
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
        with self._lock:
            self._token_bucket_state.clear()
            self._gcra_state.clear()
        if (
            self._auto_registered
            and rate_limiter_backend_registry.is_loaded
            and rate_limiter_backend_registry.get() is self
        ):
            rate_limiter_backend_registry.reset()

    def bind(self, algorithm: Algorithm) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm.

        Called once by
        [`RateLimiter`][grelmicro.resilience.RateLimiter] when
        the rate limiter is created. This is the only place that
        picks which algorithm to run. Later calls to `acquire`,
        `peek`, and `reset` use the returned strategy directly.
        """
        match algorithm:
            case TokenBucket():
                return _MemoryTokenBucket(
                    self._token_bucket_state, self._lock, algorithm
                )
            case GCRA():
                return _MemoryGCRA(self._gcra_state, self._lock, algorithm)
        assert_never(algorithm)


class _MemoryTokenBucket(RateLimiterStrategy):
    """In-memory token-bucket strategy. Private."""

    def __init__(
        self,
        state: dict[str, tuple[float, float]],
        lock: Lock,
        algorithm: TokenBucket,
    ) -> None:
        self._state = state
        self._lock = lock
        self._capacity = algorithm.capacity
        self._refill_rate = algorithm.refill_rate

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
        algorithm: GCRA,
    ) -> None:
        self._state = state
        self._lock = lock
        self._limit = algorithm.limit
        self._window = algorithm.window
        self._emission_interval = algorithm.window / algorithm.limit

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
