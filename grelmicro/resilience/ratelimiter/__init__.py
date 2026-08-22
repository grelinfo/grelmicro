"""Rate Limiter."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from dataclasses import dataclass
from string import Formatter
from typing import TYPE_CHECKING, Annotated, Any, Self, assert_never, overload
from weakref import WeakKeyDictionary

from typing_extensions import Doc

from grelmicro._app import resolve_ambient
from grelmicro._async import is_async_callable
from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    env_prefixes,
    resolve_config,
)
from grelmicro.clock import monotonic as clock_monotonic
from grelmicro.clock import sleep as clock_sleep
from grelmicro.errors import OutOfContextError
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
    from collections.abc import Awaitable, Callable

    from pydantic import Discriminator, PositiveFloat, PositiveInt

    RateLimiterConfig = Annotated[
        TokenBucketConfig | SlidingWindowConfig, Discriminator("kind")
    ]
    """Discriminated union of supported rate-limiter algorithm configurations."""

__all__ = [
    "RateLimiter",
    "RateLimiterBinding",
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


def _resolve_algorithm[C: TokenBucketConfig | SlidingWindowConfig](
    config_cls: type[C],
    name: str,
    kwargs: dict[str, object | None],
    *,
    env_load: bool | None,
) -> C:
    """Resolve one algorithm's fields from kwargs and the environment.

    The algorithm is already chosen by the factory that calls this. The
    environment only fills the fields that algorithm declares, so a
    variable naming another algorithm's field is reported rather than
    applied. See `resolve_config`.
    """
    instance_prefix, kind_prefix = env_prefixes("RATELIMITER", name)
    return resolve_config(
        config_cls,
        explicit=None,
        kwargs=kwargs,
        env_prefix=instance_prefix,
        kind_env_prefix=kind_prefix,
        env_load=env_load,
        union=_union_for_env(),
    )


def _union_for_env() -> object:
    """Return the algorithm union, for cross-arm environment reporting."""
    from pydantic import Discriminator  # noqa: PLC0415

    return Annotated[
        TokenBucketConfig | SlidingWindowConfig, Discriminator("kind")
    ]


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its bound strategy."""

    config: RateLimiterConfig
    strategy: RateLimiterStrategy | None


class RateLimiter(Reconfigurable["RateLimiterConfig"]):
    """Rate limiter with a pluggable algorithm.

    Most call sites should use the factory classmethods:
    [`RateLimiter.token_bucket`][grelmicro.resilience.RateLimiter.token_bucket]
    for burst-friendly semantics, or
    [`RateLimiter.sliding_window`][grelmicro.resilience.RateLimiter.sliding_window]
    for precise sliding-window semantics.

    When a config object already exists, pass it to
    [`RateLimiter.from_config`][grelmicro.resilience.RateLimiter.from_config]:
    a [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig] or a
    [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig].

    There is no bare constructor. Both algorithms need parameters the
    library cannot guess, so naming one is part of building the object.

    The algorithm is bound to the backend once at construction via
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    Each call to `acquire`, `peek`, or `reset` then runs the bound
    strategy directly. There is no extra algorithm lookup on each
    call.

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        """Refuse construction: a rate limiter has no default algorithm.

        Unlike `CircuitBreaker`, there is no sensible default here. Both
        algorithms need parameters the library cannot guess, so naming one
        is part of building the object.
        """
        msg = (
            "RateLimiter has no default algorithm, so it cannot be built "
            "from a bare constructor. Use RateLimiter.token_bucket(name, "
            "capacity=..., refill_rate=...), "
            "RateLimiter.sliding_window(name, limit=..., window=...), or "
            "RateLimiter.from_config(name, config)."
        )
        raise TypeError(msg)

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
        true. The factory classmethods register. `from_config` stays
        static.
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
        2. The active `Grelmicro` app is consulted on every access
           so that `micro.override(...)` blocks
           take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryRateLimiterAdapter()` for a
                per-process limiter), register a `RateLimiterComponent`
                Component, or run the call inside `async with micro:` or
                after `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        try:
            component = resolve_ambient(
                ("ratelimiter", self._backend_name or "default")
            )
        except LookupError:
            msg = (
                f"RateLimiter({self._name!r}) resolved no backend. Pass "
                f"backend= (MemoryRateLimiterAdapter() for a per-process "
                f"limiter), register a RateLimiterComponent component, or run the "
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
            PositiveInt | None,
            Doc(
                "Maximum burst size. The bucket holds at most `capacity` "
                "tokens. Required unless the value comes from env."
            ),
        ] = None,
        refill_rate: Annotated[
            PositiveFloat | None,
            Doc(
                "Tokens replenished per second, up to `capacity`. Required "
                "unless the value comes from env."
            ),
        ] = None,
        fail_open: Annotated[
            bool | None,
            Doc(
                """
                When `True`, the rate limiter returns an allowed
                result if the backend raises an error.
                """
            ),
        ] = None,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read `GREL_RATELIMITER_*` environment
                variables. When `None` (the default), follow
                `GREL_ENV_LOAD`. The environment tunes the fields of the
                algorithm chosen here and never selects the algorithm.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a token-bucket rate limiter.

        Fields not passed here resolve from `GREL_RATELIMITER_{NAME}_*`,
        then `GREL_RATELIMITER_*`, then the model default, when env
        loading is on.
        """
        self = cls.__new__(cls)
        self._setup(
            name,
            _resolve_algorithm(
                TokenBucketConfig,
                name,
                {
                    "capacity": capacity,
                    "refill_rate": refill_rate,
                    "fail_open": fail_open,
                },
                env_load=env_load,
            ),
            backend,
            register=True,
        )
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
            PositiveInt | None,
            Doc(
                "Maximum number of requests allowed per window. Required "
                "unless the value comes from env."
            ),
        ] = None,
        window: Annotated[
            PositiveFloat | None,
            Doc(
                "Window duration in seconds. Required unless the value "
                "comes from env."
            ),
        ] = None,
        fail_open: Annotated[
            bool | None,
            Doc(
                """
                When `True`, the rate limiter returns an allowed
                result if the backend raises an error.
                """
            ),
        ] = None,
        backend: Annotated[
            RateLimiterBackend | str | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read `GREL_RATELIMITER_*` environment
                variables. When `None` (the default), follow
                `GREL_ENV_LOAD`. The environment tunes the fields of the
                algorithm chosen here and never selects the algorithm.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a sliding-window rate limiter.

        Fields not passed here resolve from `GREL_RATELIMITER_{NAME}_*`,
        then `GREL_RATELIMITER_*`, then the model default, when env
        loading is on.
        """
        self = cls.__new__(cls)
        self._setup(
            name,
            _resolve_algorithm(
                SlidingWindowConfig,
                name,
                {"limit": limit, "window": window, "fail_open": fail_open},
                env_load=env_load,
            ),
            backend,
            register=True,
        )
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

    @overload
    def __call__[**P, R](
        self, fn: Callable[P, Awaitable[R]], /
    ) -> Callable[P, Awaitable[R]]: ...

    @overload
    def __call__(
        self,
        fn: None = None,
        /,
        *,
        key: str | None = None,
        key_maker: Callable[
            [Callable[..., Any], tuple[Any, ...], dict[str, Any]], str
        ]
        | None = None,
        cost: int = 1,
        max_wait: float = 0.0,
    ) -> RateLimiterBinding: ...

    def __call__(
        self,
        fn: Callable[..., Any] | None = None,
        /,
        *,
        key: Annotated[
            str | None,
            Doc(
                """
                Bucket key, rendered from the call's arguments when it
                carries a placeholder, so `key="user:{user_id}"` meters
                a call with `user_id=42` under `user:42`. A template
                names only parameters the decorated function has. A
                literal with no placeholder meters every call under the
                same key. `None` (the default) uses the limiter's
                `default` bucket. Passing both `key` and `key_maker`
                raises `TypeError`.
                """
            ),
        ] = None,
        key_maker: Annotated[
            Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any]], str]
            | None,
            Doc(
                """
                Key function receiving `(func, args, kwargs)` and
                returning the bucket key, for the fully dynamic case.
                Pass `key` instead for a template. Passing both raises
                `TypeError`.
                """
            ),
        ] = None,
        cost: Annotated[
            int,
            Doc("Tokens one call consumes."),
        ] = 1,
        max_wait: Annotated[
            float,
            Doc(
                """
                Seconds a throttled call waits for tokens before
                `RateLimitExceededError`. `0.0` (the default) raises as
                soon as the budget is spent. A positive value waits up
                to that long, then raises. The decorator never waits
                without a budget: call `limiter.wait()` directly for
                that.
                """
            ),
        ] = 0.0,
    ) -> Callable[..., Any] | RateLimiterBinding:
        """Decorate a function so each call consumes tokens first.

        `@limiter` meters the whole function under the limiter's
        `default` bucket. `@limiter(key=...)` returns a
        `RateLimiterBinding`, which decorates and which
        `Stack(patterns=[...])` accepts.

        Raises:
            TypeError: If both `key` and `key_maker` are passed, or the
                decorated function is not async.
            ValueError: If `max_wait` is negative, or a `key` template
                names a parameter the function does not have.
        """
        binding = RateLimiterBinding(
            self,
            key=key,
            key_maker=key_maker,
            cost=cost,
            max_wait=max_wait,
        )
        if fn is None:
            return binding
        return binding(fn)

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


_DEFAULT_KEY = "default"
"""Bucket a call is metered under when no key is given."""


def _named(fn: object) -> str:
    """Return the name of a decorated function, for a message."""
    return getattr(fn, "__qualname__", None) or repr(fn)


def _template_fields(template: str) -> list[str]:
    """Return the parameter names a key template reads.

    Raises:
        ValueError: If the template reads a positional field, which
            names no parameter.
    """
    fields = []
    for _, field, spec, _ in Formatter().parse(template):
        if field is not None:
            fields.append(field.split(".")[0].split("[")[0])
        if spec:
            fields.extend(_template_fields(spec))
    if any(name == "" or name.isdigit() for name in fields):
        msg = (
            f"Rate limiter key template {template!r} reads a "
            "positional field. Name the parameter instead, as in "
            '"user:{user_id}".'
        )
        raise ValueError(msg)
    return fields


class RateLimiterBinding:
    """A rate limiter bound to a key, a cost, and a wait budget.

    `limiter(key="user:{user_id}")` returns one. It decorates an async
    function, and `Stack(patterns=[...])` accepts it wherever it accepts
    a bare `RateLimiter`.

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    __slots__ = (
        "_cost",
        "_fields",
        "_key",
        "_key_maker",
        "_limiter",
        "_max_wait",
        "_reads_signature",
        "_resolvers",
    )

    def __init__(
        self,
        limiter: Annotated[
            RateLimiter,
            Doc("The limiter that meters the call."),
        ],
        /,
        *,
        key: str | None = None,
        key_maker: Callable[
            [Callable[..., Any], tuple[Any, ...], dict[str, Any]], str
        ]
        | None = None,
        cost: int = 1,
        max_wait: float = 0.0,
    ) -> None:
        """Bind a limiter to one key, cost, and wait budget.

        Raises:
            TypeError: If both `key` and `key_maker` are passed, or
                `max_wait` is `None`.
            ValueError: If `cost` is below 1, `max_wait` is negative,
                or `key` reads a positional field.
        """
        if key is not None and key_maker is not None:
            msg = (
                "Pass key or key_maker, not both. `key` renders a "
                "template from the call's arguments, `key_maker` "
                "computes the key itself."
            )
            raise TypeError(msg)
        if cost < 1:
            msg = (
                "cost is a number of tokens, so it is at least 1, got "
                f"{cost!r}."
            )
            raise ValueError(msg)
        if max_wait is None:
            msg = (
                "max_wait is a number of seconds here, never None. "
                "`limiter.wait(max_wait=None)` waits forever, and a "
                "wait with no bound has nothing above it to stop it "
                "inside a stack. Use 0.0 to raise as soon as the "
                "budget is spent, or a positive number of seconds."
            )
            raise TypeError(msg)
        if max_wait < 0:
            msg = (
                "max_wait is a number of seconds, so it cannot be "
                f"negative, got {max_wait!r}. Use 0.0 to raise as soon "
                "as the budget is spent."
            )
            raise ValueError(msg)
        self._limiter = limiter
        self._key = key
        self._fields = (
            _template_fields(key)
            if key_maker is None and key is not None and "{" in key
            else ()
        )
        self._reads_signature = bool(self._fields)
        self._key_maker = key_maker
        self._cost = cost
        self._max_wait = max_wait
        self._resolvers: WeakKeyDictionary[
            Callable[..., Any],
            Callable[[tuple[Any, ...], dict[str, Any]], str],
        ] = WeakKeyDictionary()

    @property
    def limiter(self) -> RateLimiter:
        """The bound rate limiter."""
        return self._limiter

    def __repr__(self) -> str:
        """Return the binding with the key it meters under."""
        key = self._key if self._key is not None else _DEFAULT_KEY
        if self._key_maker is not None:
            key = getattr(self._key_maker, "__qualname__", "key_maker")
        return f"<RateLimiterBinding {self._limiter.name!r} key={key!r}>"

    def _resolver(
        self, fn: Callable[..., Any]
    ) -> Callable[[tuple[Any, ...], dict[str, Any]], str]:
        """Return the key resolver for one decorated function.

        Only a template resolver is worth keeping: it is the one that
        reads a signature, and the one that closes over the signature
        rather than over the callable, so it can be held without
        keeping that callable alive. Two callables that compare equal
        share one safely, because it renders from the arguments and
        never calls them. A callable that cannot be a weak key is
        resolved fresh.

        Raises:
            ValueError: If the template names a parameter `fn` has not.
        """
        if not self._reads_signature:
            return self._build_resolver(fn)
        cache_key = getattr(fn, "__func__", fn)
        try:
            cached = self._resolvers.get(cache_key)
        except TypeError:
            return self._build_resolver(fn)
        if cached is not None:
            return cached
        resolver = self._build_resolver(fn)
        self._resolvers[cache_key] = resolver
        return resolver

    def _build_resolver(
        self, fn: Callable[..., Any]
    ) -> Callable[[tuple[Any, ...], dict[str, Any]], str]:
        """Build the key resolver for one decorated function.

        Raises:
            ValueError: If the template names a parameter `fn` has not.
        """
        key_maker = self._key_maker
        if key_maker is not None:
            return lambda args, kwargs: key_maker(fn, args, kwargs)
        key = self._key
        if key is None:
            return lambda _args, _kwargs: _DEFAULT_KEY
        if not self._fields:
            constant = key.format_map({}) if "{" in key else key
            return lambda _args, _kwargs: constant
        signature = inspect.signature(fn)
        unknown = sorted(set(self._fields) - set(signature.parameters))
        if unknown:
            names = ", ".join(unknown)
            msg = (
                f"Rate limiter key template {key!r} names "
                f"{names}, which {_named(fn)} does not take."
            )
            raise ValueError(msg)

        name = _named(fn)

        def render(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError as error:
                msg = f"{name}() {error}"
                raise TypeError(msg) from None
            bound.apply_defaults()
            return key.format_map(bound.arguments)

        return render

    def _admitter(
        self, fn: Callable[..., Any]
    ) -> Callable[[tuple[Any, ...], dict[str, Any]], Awaitable[object]]:
        """Return the coroutine function admitting one call of `fn`."""
        resolve = self._resolver(fn)
        limiter = self._limiter
        cost = self._cost
        max_wait = self._max_wait
        if max_wait:

            async def wait_for_tokens(
                args: tuple[Any, ...], kwargs: dict[str, Any]
            ) -> object:
                return await limiter.wait(
                    key=resolve(args, kwargs), cost=cost, max_wait=max_wait
                )

            return wait_for_tokens

        async def take_tokens(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> object:
            return await limiter.acquire_or_raise(
                key=resolve(args, kwargs), cost=cost
            )

        return take_tokens

    def __call__[**P, R](
        self, fn: Callable[P, Awaitable[R]], /
    ) -> Callable[P, Awaitable[R]]:
        """Decorate `fn` so each call consumes tokens first.

        Raises:
            TypeError: If `fn` is not async.
            ValueError: If a `key` template names a parameter `fn` has
                not.
        """
        if not is_async_callable(fn):
            msg = (
                "RateLimiter only decorates async functions. Consuming "
                "tokens reaches the backend, and waiting for them "
                f"needs the event loop, got {fn!r}."
            )
            raise TypeError(msg)
        admit = self._admitter(fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            await admit(args, kwargs)
            return await fn(*args, **kwargs)

        return async_wrapper
