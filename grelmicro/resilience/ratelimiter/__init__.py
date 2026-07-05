"""Rate Limiter."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Self, assert_never

from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._config import Reconfigurable, default_env_prefix
from grelmicro.clock import monotonic as clock_monotonic
from grelmicro.clock import sleep as clock_sleep
from grelmicro.metrics import _emit
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

if TYPE_CHECKING:
    from pydantic import Discriminator, PositiveFloat, PositiveInt

    RateLimiterConfig = Annotated[
        TokenBucketConfig | SlidingWindowConfig, Discriminator("kind")
    ]
    """Discriminated union of supported rate-limiter algorithm configurations."""

__all__ = [
    "RateLimiter",
    "RateLimiterConfig",
    "SlidingWindowConfig",
    "TokenBucketConfig",
]


def __getattr__(name: str) -> object:
    """PEP 562 lazy loader for the discriminated-union alias."""
    if name == "RateLimiterConfig":
        from pydantic import Discriminator  # noqa: PLC0415

        return Annotated[
            TokenBucketConfig | SlidingWindowConfig, Discriminator("kind")
        ]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


logger = logging.getLogger(__name__)

_MIN_POLL_INTERVAL = 0.005
"""Floor for the `wait` poll sleep, avoiding a busy-loop on a zero or
coarse `retry_after` from a distributed backend."""


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its bound strategy."""

    config: RateLimiterConfig
    strategy: RateLimiterStrategy | None


class RateLimiter(Reconfigurable["RateLimiterConfig"]):
    """Rate limiter with a pluggable algorithm.

    Most Python call sites should use the factory classmethods:
    [`RateLimiter.token_bucket`][grelmicro.resilience.RateLimiter.token_bucket]
    for burst-friendly semantics or
    [`RateLimiter.sliding_window`][grelmicro.resilience.RateLimiter.sliding_window] for
    precise sliding-window semantics.

    Construct it directly with the instance name and a discriminated
    algorithm configuration when a config object already exists:
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    for burst-friendly semantics, or
    [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig] for
    precise sliding-window semantics.

    The algorithm is bound to the backend once at construction via
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    Each call to `acquire`, `peek`, or `reset` then runs the bound
    strategy directly. There is no extra algorithm lookup on each
    call.

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
                or [`RateLimiter.sliding_window`][grelmicro.resilience.RateLimiter.sliding_window]
                factory classmethods. Pass a config directly when it
                is already assembled elsewhere, for example from YAML
                or a `pydantic-settings` tree.

                Pass a
                [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
                or a
                [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig].
                Both carry algorithm parameters plus the shared
                `fail_open` setting. The classes share a
                discriminated `kind` field so serialization
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
        self._setup(name, config, backend, register=False)

    def _setup(
        self,
        name: str,
        config: RateLimiterConfig,
        backend: RateLimiterBackend | str | None,
        *,
        register: bool = False,
    ) -> None:
        """Wire the config and runtime deps onto the instance.

        Registers the instance for external reload under
        `GREL_RATELIMITER_` for the default instance
        (`GREL_RATELIMITER_{NAME}_` for a named one) when `register` is
        true. The factory
        classmethods register. The declarative paths (a pre-built config
        passed to the constructor, or `from_config`) stay static.
        """
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
        if register:
            self._track_reconfigure(default_env_prefix("RATELIMITER", name))

    @property
    def name(self) -> str:
        """Return the rate limiter identity."""
        return self._name

    @property
    def backend(self) -> RateLimiterBackend:
        """Bound rate-limiter backend, resolved on each call.

        Resolution order:
        1. An explicit `backend=` passed at construction wins.
        2. The active `Grelmicro` app is consulted via
           `Grelmicro.current()` so that `micro.override(...)` blocks
           take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryRateLimiterAdapter()` for a
                per-process limiter), register a `RateLimiterRegistry`
                Component, or run the call inside `async with micro:` or
                after `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            NoActiveAppError,
        )
        from grelmicro.errors import OutOfContextError  # noqa: PLC0415

        try:
            component = Grelmicro.current().get(
                "ratelimiter", self._backend_name or "default"
            )
        except (NoActiveAppError, ComponentNotRegisteredError):
            msg = (
                f"RateLimiter({self._name!r}) resolved no backend. Pass "
                f"backend= (MemoryRateLimiterAdapter() for a per-process "
                f"limiter), register a RateLimiterRegistry component, or run the "
                f"call inside `async with micro:` or after `micro.install(app)`."
            )
            raise OutOfContextError(msg) from None
        return component.backend

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

        Use this when configuration is assembled declaratively at
        startup and the simple factory classmethods are not the right
        fit. This declarative path opts out of live reload: the instance
        is not addressable by `ExternalConfig` and stays on the config it
        was built with.
        """
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

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
        [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
        internally and forwards to the constructor.
        """
        config = TokenBucketConfig(
            capacity=capacity,
            refill_rate=refill_rate,
            fail_open=fail_open,
        )
        self = cls.__new__(cls)
        self._setup(name, config, backend, register=True)
        return self

    @classmethod
    def sliding_window(
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
        """Construct a sliding-window rate limiter.

        Convenience factory for the common case. Builds a
        [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]
        internally and forwards to the constructor.
        """
        config = SlidingWindowConfig(
            limit=limit, window=window, fail_open=fail_open
        )
        self = cls.__new__(cls)
        self._setup(name, config, backend, register=True)
        return self

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
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
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
            result = await strategy.acquire(key=self._full_key(key), cost=cost)
        except Exception as exc:
            if config.fail_open:
                self._log_fail_open(key, exc)
                return _build_fallback(config)
            raise
        _emit.incr(
            "grelmicro.rate_limiter.decisions",
            **{
                "rate_limiter.name": self._name,
                "decision": "allowed" if result.allowed else "limited",
            },
        )
        return result

    async def acquire_or_raise(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
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

    async def allow(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
    ) -> bool:
        """Consume tokens and return whether the request is within the limit.

        The boolean shortcut over `acquire`, for the common branch:

        ```python
        if await limiter.allow(key="user-1"):
            ...  # served
        else:
            ...  # throttled
        ```

        Use `acquire` instead when you need the `retry_after` or `remaining`
        metadata on the deny branch.
        """
        return (await self.acquire(key=key, cost=cost)).allowed

    async def peek(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
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
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
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

    async def wait(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
                " Defaults to `default` for the single-bucket case."
                " The limiter's `name` already namespaces the backend"
                " key, so the default bucket is `name:default`."
            ),
        ] = "default",
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
        max_wait: Annotated[
            float | None,
            Doc(
                "Maximum number of seconds to wait before giving up."
                " `None` (the default) waits indefinitely."
            ),
        ] = None,
    ) -> RateLimitResult:
        """Wait until tokens are available, then consume them.

        Polls `acquire` on the clock seam, sleeping `retry_after`
        between attempts, until the request is admitted. A denied
        `acquire` consumes nothing, so retrying is safe.

        With `max_wait` set, gives up once the budget would be exceeded
        and raises `RateLimitExceededError`. The default waits forever:

        ```python
        await limiter.wait(key="user-1")
        result = await limiter.wait(key="user-1", cost=3, max_wait=2.0)
        ```

        The wait runs on the clock seam, so `VirtualClock` drives it in
        tests without real sleeping.

        Returns:
            The allowed RateLimitResult once tokens are consumed.

        Raises:
            ValueError: If `cost` is not between 1 and the algorithm's
                limit/capacity. Guards the otherwise unsatisfiable wait
                when `cost` exceeds capacity.
            RateLimitExceededError: If `max_wait` elapses before the
                request is admitted.
        """
        _validate_cost(cost, _config_limit(self._state.config))
        deadline = None if max_wait is None else clock_monotonic() + max_wait
        while True:
            result = await self.acquire(key=key, cost=cost)
            if result.allowed:
                return result
            delay = result.retry_after
            if deadline is not None:
                remaining = deadline - clock_monotonic()
                if remaining <= 0 or delay > remaining:
                    raise RateLimitExceededError(
                        key=key,
                        retry_after=result.retry_after,
                    )
            await clock_sleep(max(delay, _MIN_POLL_INTERVAL))

    async def _apply_reconfigure(
        self,
        new_config: RateLimiterConfig,
    ) -> None:
        """Publish the new config and clear the cached strategy.

        The next call rebinds the strategy through `_resolve_strategy`
        with the freshly published config, matching the circuit breaker.
        """
        self._state = _State(config=new_config, strategy=None)


def _config_limit(config: RateLimiterConfig) -> int:
    """Return the algorithm's nominal limit for the given config."""
    match config:
        case TokenBucketConfig():
            return config.capacity
        case SlidingWindowConfig():
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
