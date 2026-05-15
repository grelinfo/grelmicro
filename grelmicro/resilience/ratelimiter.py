"""Rate Limiter."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated, Self, assert_never

from pydantic import PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._config import Reconfigurable
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
from grelmicro.resilience.errors import RateLimitExceededError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its bound strategy."""

    config: RateLimiterConfig
    strategy: RateLimiterStrategy | None


class RateLimiter(Reconfigurable[RateLimiterConfig]):
    """Rate limiter with a pluggable algorithm.

    Most Python call sites should use the factory classmethods:
    [`RateLimiter.token_bucket`][grelmicro.resilience.RateLimiter.token_bucket]
    for burst-friendly semantics or
    [`RateLimiter.gcra`][grelmicro.resilience.RateLimiter.gcra] for
    precise sliding-window semantics.

    Construct it directly with the instance name and a discriminated
    algorithm configuration when a config object already exists:
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    for burst-friendly semantics, or
    [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig] for
    precise sliding-window semantics.

    The algorithm is bound to the backend once at construction via
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    Each call to `acquire`, `peek`, or `reset` then runs the bound
    strategy directly. There is no extra algorithm lookup on each
    call.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter
    from grelmicro.resilience.memory import MemoryRateLimiterAdapter

    MemoryRateLimiterAdapter()
    api = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)


    async def handle(user_id: str) -> None:
        result = await api.acquire(key=user_id)
        if not result.allowed:
            raise RuntimeError("rate limited")
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                The name of the rate limiter instance.

                Acts as the instance identity. Used as the key
                prefix on the backend and exposed via the `name`
                property.
                """
            ),
        ],
        config: Annotated[
            RateLimiterConfig,
            Doc(
                """
                The algorithm configuration.

                Most callers should prefer the
                [`RateLimiter.token_bucket`][grelmicro.resilience.RateLimiter.token_bucket]
                or [`RateLimiter.gcra`][grelmicro.resilience.RateLimiter.gcra]
                factory classmethods. Pass a config directly when it
                is already assembled elsewhere, for example from YAML
                or a `pydantic-settings` tree.

                Pass a
                [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
                or a
                [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig].
                Both carry algorithm parameters plus the shared
                `fail_open` setting. The classes share a
                discriminated `type` field so serialization
                round-trips and pydantic-settings composition both
                work.
                """
            ),
        ],
        *,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.

                Set this to skip the global registry, for example
                in tests or when running several backends at the
                same time.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the rate limiter."""
        self._name = name
        self._backend: RateLimiterBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )
        self._reconfigure_lock = asyncio.Lock()
        self._config = config
        self._state = _State(config=config, strategy=None)

    @property
    def name(self) -> str:
        """Return the rate limiter identity."""
        return self._name

    @property
    def backend(self) -> RateLimiterBackend:
        """Bound rate-limiter backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the active `Grelmicro` app is
        consulted via `Grelmicro.current()` on every access so that
        `micro.override(RateLimit(...))` blocks take effect.
        """
        if self._backend is not None:
            return self._backend
        rl = Grelmicro.current().get(
            "ratelimiter", self._backend_name or "default"
        )
        return rl.backend

    def _resolve_strategy(self, state: _State) -> RateLimiterStrategy:
        """Bind the algorithm config to the backend and republish the snapshot."""
        strategy = self.backend.bind(state.config)
        self._state = _State(config=state.config, strategy=strategy)
        return strategy

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc("The name of the rate limiter instance."),
        ],
        config: Annotated[
            RateLimiterConfig,
            Doc("The pre-built algorithm configuration."),
        ],
        *,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a `RateLimiter` from a name and a pre-built config.

        Equivalent to ``RateLimiter(name, config, backend=backend)``.
        Use this when configuration is assembled declaratively at
        startup and the simple factory classmethods are not the right
        fit.
        """
        return cls(name, config, backend=backend)

    @classmethod
    def token_bucket(
        cls,
        name: Annotated[
            str,
            Doc("The name of the rate limiter instance."),
        ],
        *,
        capacity: Annotated[
            PositiveInt,
            Doc(
                "Maximum burst size. The bucket holds at most `capacity` tokens."
            ),
        ],
        refill_rate: Annotated[
            PositiveFloat,
            Doc("Tokens replenished per second, up to `capacity`."),
        ],
        fail_open: Annotated[
            bool,
            Doc(
                """
                When `True`, the rate limiter returns an allowed
                result if the backend raises an error.
                """
            ),
        ] = False,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a token-bucket rate limiter.

        Convenience factory for the common case. Builds a
        [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
        internally and forwards to the constructor.
        """
        config = TokenBucketConfig(
            capacity=capacity,
            refill_rate=refill_rate,
            fail_open=fail_open,
        )
        return cls(name, config, backend=backend)

    @classmethod
    def gcra(
        cls,
        name: Annotated[
            str,
            Doc("The name of the rate limiter instance."),
        ],
        *,
        limit: Annotated[
            PositiveInt,
            Doc("Maximum number of requests allowed per window."),
        ],
        window: Annotated[
            PositiveFloat,
            Doc("Window duration in seconds."),
        ],
        fail_open: Annotated[
            bool,
            Doc(
                """
                When `True`, the rate limiter returns an allowed
                result if the backend raises an error.
                """
            ),
        ] = False,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a GCRA (sliding-window) rate limiter.

        Convenience factory for the common case. Builds a
        [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig]
        internally and forwards to the constructor.
        """
        config = GCRAConfig(limit=limit, window=window, fail_open=fail_open)
        return cls(name, config, backend=backend)

    def _log_fail_open(
        self,
        key: str,
        exc: Exception,
    ) -> None:
        """Log a fail-open warning for a backend error."""
        logger.warning(
            "Rate limiter '%s' backend error, failing open for key '%s'",
            self._name,
            key,
            exc_info=exc,
        )

    def _full_key(self, key: str) -> str:
        return f"{self._name}:{key}"

    async def acquire(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
    ) -> RateLimitResult:
        """Check rate limit and consume tokens if allowed.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.

        Raises:
            ValueError: If `cost` is not between 1 and the
                algorithm's limit/capacity.
        """
        state = self._state
        config = state.config
        _validate_cost(cost, _config_limit(config))
        strategy = state.strategy or self._resolve_strategy(state)
        try:
            return await strategy.acquire(key=self._full_key(key), cost=cost)
        except Exception as exc:
            if config.fail_open:
                self._log_fail_open(key, exc)
                return _build_fallback(config)
            raise

    async def acquire_or_raise(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
    ) -> RateLimitResult:
        """Check rate limit, raise if exceeded.

        Returns:
            RateLimitResult if allowed.

        Raises:
            RateLimitExceededError: If the rate limit is exceeded.
        """
        result = await self.acquire(key=key, cost=cost)
        if not result.allowed:
            raise RateLimitExceededError(
                key=key,
                retry_after=result.retry_after,
            )
        return result

    async def peek(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
    ) -> RateLimitResult:
        """Check rate limit state without consuming tokens.

        Returns:
            RateLimitResult reflecting the current state.
            `allowed` indicates whether the next acquire would
            succeed.
        """
        state = self._state
        config = state.config
        strategy = state.strategy or self._resolve_strategy(state)
        try:
            return await strategy.peek(key=self._full_key(key))
        except Exception as exc:
            if config.fail_open:
                self._log_fail_open(key, exc)
                return _build_fallback(config)
            raise

    async def reset(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
    ) -> None:
        """Delete rate limit state for a key, restoring full quota.

        Idempotent: resetting a nonexistent key is a no-op.
        """
        state = self._state
        strategy = state.strategy or self._resolve_strategy(state)
        try:
            await strategy.reset(key=self._full_key(key))
        except Exception as exc:
            if state.config.fail_open:
                self._log_fail_open(key, exc)
                return
            raise

    async def _apply_reconfigure(
        self,
        new_config: RateLimiterConfig,
    ) -> None:
        """Bind the new strategy and publish a fresh snapshot in one assignment."""
        new_strategy = self.backend.bind(new_config)
        self._state = _State(config=new_config, strategy=new_strategy)


def _config_limit(config: RateLimiterConfig) -> int:
    """Return the algorithm's nominal limit for the given config."""
    match config:
        case TokenBucketConfig():
            return config.capacity
        case GCRAConfig():
            return config.limit
        case _ as unknown:  # pragma: no cover
            assert_never(unknown)


def _build_fallback(config: RateLimiterConfig) -> RateLimitResult:
    """Build the fail-open fallback result for the given algorithm config."""
    limit_value = _config_limit(config)
    return RateLimitResult(
        allowed=True,
        limit=limit_value,
        remaining=limit_value,
        retry_after=0.0,
        reset_after=0.0,
    )


def _validate_cost(cost: int, limit: int) -> None:
    """Validate that cost is within `[1, limit]`."""
    if cost < 1 or cost > limit:
        msg = f"cost must be between 1 and {limit}, got {cost}"
        raise ValueError(msg)
