"""Shield resilience pattern."""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger
from typing import Annotated, Any, Self, TypeVar

from pydantic import Discriminator, PositiveFloat
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    env_load_default,
    env_segment,
)
from grelmicro.resilience.errors import ResilienceError
from grelmicro.resilience.shield._adaptive_gate import _AdaptiveGate
from grelmicro.resilience.shield._api import ApiShieldConfig
from grelmicro.resilience.shield._internal import InternalShieldConfig
from grelmicro.resilience.shield._key import default_cache_key
from grelmicro.resilience.shield._profile import _BaseShieldConfig
from grelmicro.resilience.shield._retry_budget import _RetryBudget
from grelmicro.resilience.shield._slow import SlowShieldConfig
from grelmicro.resilience.shield._timeout_estimator import _TimeoutEstimator

__all__ = ["Shield", "ShieldConfig"]

logger = getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

ShieldConfig = Annotated[
    InternalShieldConfig | ApiShieldConfig | SlowShieldConfig,
    Discriminator("kind"),
]
"""Discriminated union over the three Shield profile configs."""


_MAX_ATTEMPTS: int = 4
"""Total attempts: one initial call plus three retries."""


_GIVE_UP_BUDGET = "budget exhausted"
_GIVE_UP_ATTEMPTS = "attempts exhausted"
_GIVE_UP_NON_RETRY = "non-retryable exception"


_PROFILE_BY_NAME: dict[str, type[_BaseShieldConfig]] = {
    "internal": InternalShieldConfig,
    "api": ApiShieldConfig,
    "slow": SlowShieldConfig,
}


def _load_profile_from_env(name: str) -> str:
    """Return the profile name from env, defaulting to `api`."""
    env_key = f"GREL_SHIELD_{env_segment(name)}_PROFILE"
    value = os.environ.get(env_key, "").strip().lower()
    if value and value not in _PROFILE_BY_NAME:
        msg = (
            f"{env_key}={value!r} is not a valid profile. "
            f"Expected one of: internal, api, slow."
        )
        raise ValueError(msg)
    return value or "api"


def _resolve_config_from_env(
    name: str,
    *,
    timeout_errors: Any,  # noqa: ANN401
    max_rate: float | None,
    cache: Any,  # noqa: ANN401
    cache_key: Callable[..., str] | None,
    fallback: Callable[..., Any] | None,
) -> _BaseShieldConfig:
    """Build a `_BaseShieldConfig` reading defaults from environment variables."""
    profile = _load_profile_from_env(name)
    cls = _PROFILE_BY_NAME[profile]
    env_prefix = f"GREL_SHIELD_{env_segment(name)}_"
    kwargs: dict[str, Any] = {"kind": profile}
    if timeout_errors is not None:
        kwargs["timeout_errors"] = timeout_errors
    else:
        env_value = os.environ.get(f"{env_prefix}TIMEOUT_ERRORS")
        if env_value is not None:
            kwargs["timeout_errors"] = env_value
    if max_rate is not None:
        kwargs["max_rate"] = max_rate
    else:
        env_value = os.environ.get(f"{env_prefix}MAX_RATE")
        if env_value is not None and env_value.strip() != "":
            kwargs["max_rate"] = float(env_value)
    if cache is not None:
        kwargs["cache"] = cache
    if cache_key is not None:
        kwargs["cache_key"] = cache_key
    if fallback is not None:
        kwargs["fallback"] = fallback
    return cls.model_validate(kwargs)


def _build_config(
    name: str,
    *,
    config: _BaseShieldConfig | None,
    profile: str,
    timeout_errors: Any,  # noqa: ANN401
    max_rate: float | None,
    cache: Any,  # noqa: ANN401
    cache_key: Callable[..., str] | None,
    fallback: Callable[..., Any] | None,
    env_load: bool | None,
) -> _BaseShieldConfig:
    """Resolve a `_BaseShieldConfig` from kwargs, an explicit config, or env."""
    explicit_kwargs = any(
        value is not None
        for value in (
            timeout_errors,
            max_rate,
            cache,
            cache_key,
            fallback,
        )
    )
    if config is not None:
        if explicit_kwargs:
            msg = "pass a pre-built config OR individual kwargs, not both"
            raise TypeError(msg)
        return config
    if env_load is None:
        env_load = env_load_default()
    if env_load:
        return _resolve_config_from_env(
            name,
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
        )
    cls = _PROFILE_BY_NAME[profile]
    kwargs: dict[str, Any] = {"kind": profile}
    if timeout_errors is not None:
        kwargs["timeout_errors"] = timeout_errors
    if max_rate is not None:
        kwargs["max_rate"] = max_rate
    if cache is not None:
        kwargs["cache"] = cache
    if cache_key is not None:
        kwargs["cache_key"] = cache_key
    if fallback is not None:
        kwargs["fallback"] = fallback
    return cls.model_validate(kwargs)


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot of the published Shield configuration."""

    config: _BaseShieldConfig
    effective_timeout_errors: tuple[type[BaseException], ...]


async def _maybe_await(value: Any) -> Any:  # noqa: ANN401
    """Await `value` when it is a coroutine, return it as-is otherwise."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _safe_cache_set(cache: Any, key: str, value: Any) -> None:  # noqa: ANN401
    """Write `value` to `cache` under `key`. Swallow every exception at debug."""
    try:
        await cache.set(key, value)
    except Exception:  # noqa: BLE001
        logger.debug("shield: cache write failed", exc_info=True)


class Shield(Reconfigurable[_BaseShieldConfig]):
    """Shield resilience pattern.

    Wraps a single async callable with:

    - A per-attempt timeout estimated from the rolling p95 of the last
      32 successful latencies.
    - Exponential-jittered retries gated by a consecutive-failure budget.
    - A CUBIC-style adaptive rate limiter that engages on the first
      slow-down and ramps back gradually.
    - Optional cache and fallback recovery paths on give-up.

    One `Shield` instance covers one logical dependency. Multiple
    functions hitting the same dependency share one `Shield` and
    therefore one retry budget and one CUBIC controller.

    Read more in the [Shield](../resilience/shield.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "Registration name of the Shield instance. Appears in "
                "logs, metrics, and PEP 678 notes attached on give-up."
            ),
        ],
        config: Annotated[
            _BaseShieldConfig | None,
            Doc(
                "A pre-built profile config "
                "([`InternalShieldConfig`][grelmicro.resilience.InternalShieldConfig], "
                "[`ApiShieldConfig`][grelmicro.resilience.ApiShieldConfig], "
                "[`SlowShieldConfig`][grelmicro.resilience.SlowShieldConfig]). "
                "Mutually exclusive with the per-field kwargs."
            ),
        ] = None,
        *,
        timeout_errors: Annotated[
            tuple[type[BaseException], ...] | None,
            Doc(
                "Exception classes treated as transient slow-downs. "
                "`TimeoutError` is always appended. Default `(TimeoutError,)`."
            ),
        ] = None,
        max_rate: Annotated[
            PositiveFloat | None,
            Doc(
                "Optional hard ceiling on the adaptive bucket's rate "
                "in tokens per second."
            ),
        ] = None,
        cache: Annotated[  # noqa: ANN401
            Any,
            Doc(
                "Optional cache instance read on give-up and written "
                "fire-and-forget on success."
            ),
        ] = None,
        cache_key: Annotated[
            Callable[..., str] | None,
            Doc(
                "Optional callable returning the cache key for a call. "
                'Defaults to `f"{name}:{stable_hash(args, kwargs)}"`.'
            ),
        ] = None,
        fallback: Annotated[
            Callable[[BaseException], Any]
            | Callable[[BaseException], Awaitable[Any]]
            | None,
            Doc(
                "Optional callable invoked on give-up when the cache "
                "path returns nothing. Receives the underlying exception."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults to the "
                "process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
        time_source: Annotated[
            Callable[[], float] | None,
            Doc("Monotonic clock for tests. Defaults to `time.monotonic`."),
        ] = None,
        random_source: Annotated[
            Callable[[], float] | None,
            Doc(
                "Uniform `[0, 1)` random function for backoff jitter. "
                "Defaults to `random.random`."
            ),
        ] = None,
    ) -> None:
        """Initialize the Shield instance with the `api` profile by default."""
        self._setup(
            name=name,
            config=_build_config(
                name,
                config=config,
                profile="api",
                timeout_errors=timeout_errors,
                max_rate=max_rate,
                cache=cache,
                cache_key=cache_key,
                fallback=fallback,
                env_load=env_load,
            ),
            time_source=time_source,
            random_source=random_source,
        )

    def _setup(
        self,
        *,
        name: str,
        config: _BaseShieldConfig,
        time_source: Callable[[], float] | None,
        random_source: Callable[[], float] | None,
    ) -> None:
        """Wire the resolved config and helpers onto the instance."""
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._time = time_source or time.monotonic
        self._random = random_source or random.random
        self._retry_budget = _RetryBudget(
            capacity=config.max_consecutive_failures
        )
        cap = config.max_rate or config.max_rate_cap_default
        self._adaptive_gate = _AdaptiveGate(
            initial_max_rate=config.initial_max_rate,
            capacity=config.adaptive_burst_capacity,
            min_rate_floor=config.min_rate_floor,
            max_rate_cap=cap,
            time_source=self._time,
        )
        self._timeout_estimator = _TimeoutEstimator(
            initial_timeout=config.initial_timeout,
            clamp_min=config.timeout_clamp_min,
            clamp_max=config.timeout_clamp_max,
        )
        self._state = _State(
            config=config,
            effective_timeout_errors=config.effective_timeout_errors(),
        )

    @property
    def name(self) -> str:
        """Return the Shield instance name."""
        return self._name

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("Name of the Shield instance.")],
        config: Annotated[
            _BaseShieldConfig,
            Doc("The pre-built Shield profile configuration."),
        ],
    ) -> Self:
        """Construct a `Shield` from a name and a pre-built profile config."""
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            name=name,
            config=config,
            time_source=None,
            random_source=None,
        )
        return instance

    @classmethod
    def internal(
        cls,
        name: Annotated[str, Doc("Name of the Shield instance.")],
        *,
        timeout_errors: tuple[type[BaseException], ...] | None = None,
        max_rate: PositiveFloat | None = None,
        cache: Any = None,  # noqa: ANN401
        cache_key: Callable[..., str] | None = None,
        fallback: Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None = None,
    ) -> Self:
        """Construct a Shield with the `internal` profile."""
        return cls._make(
            name=name,
            profile="internal",
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
        )

    @classmethod
    def api(
        cls,
        name: Annotated[str, Doc("Name of the Shield instance.")],
        *,
        timeout_errors: tuple[type[BaseException], ...] | None = None,
        max_rate: PositiveFloat | None = None,
        cache: Any = None,  # noqa: ANN401
        cache_key: Callable[..., str] | None = None,
        fallback: Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None = None,
    ) -> Self:
        """Construct a Shield with the `api` profile (the default)."""
        return cls._make(
            name=name,
            profile="api",
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
        )

    @classmethod
    def slow(
        cls,
        name: Annotated[str, Doc("Name of the Shield instance.")],
        *,
        timeout_errors: tuple[type[BaseException], ...] | None = None,
        max_rate: PositiveFloat | None = None,
        cache: Any = None,  # noqa: ANN401
        cache_key: Callable[..., str] | None = None,
        fallback: Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None = None,
    ) -> Self:
        """Construct a Shield with the `slow` profile."""
        return cls._make(
            name=name,
            profile="slow",
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
        )

    @classmethod
    def _make(
        cls,
        *,
        name: str,
        profile: str,
        timeout_errors: tuple[type[BaseException], ...] | None,
        max_rate: PositiveFloat | None,
        cache: Any,  # noqa: ANN401
        cache_key: Callable[..., str] | None,
        fallback: Callable[..., Any] | None,
    ) -> Self:
        """Build a Shield bypassing env reads, with a profile override."""
        config = _build_config(
            name,
            config=None,
            profile=profile,
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
            env_load=False,
        )
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            name=name,
            config=config,
            time_source=None,
            random_source=None,
        )
        return instance

    # ------------------------------------------------------------------ run

    async def run(
        self,
        fn: Annotated[
            Callable[..., Awaitable[Any]],
            Doc("Async callable to invoke under this Shield."),
        ],
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Run `fn(*args, **kwargs)` through this Shield instance."""
        if not inspect.iscoroutinefunction(fn) and not _is_async_callable(fn):
            msg = (
                "Shield.run requires an async callable. "
                f"Got {fn!r}. Wrap sync code in asyncio.to_thread(...)."
            )
            raise TypeError(msg)
        return await self._execute(fn, args, kwargs)

    def __call__(
        self,
        fn: Annotated[
            Callable[..., Awaitable[Any]],
            Doc("Async function to decorate."),
        ],
    ) -> Callable[..., Awaitable[Any]]:
        """Decorate `fn` so each call runs through this Shield."""
        if not inspect.iscoroutinefunction(fn):
            msg = (
                "Shield only decorates async functions. "
                f"Got {fn!r}. Wrap sync code in asyncio.to_thread(...)."
            )
            raise TypeError(msg)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            return await self._execute(fn, args, kwargs)

        return wrapper

    # ------------------------------------------------------------- internals

    async def _execute(  # noqa: C901
        self,
        fn: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        state = self._state
        started = self._time()
        retries_consumed = 0
        attempt = 0
        last_exc: BaseException | None = None
        give_up_reason = _GIVE_UP_ATTEMPTS
        while attempt < _MAX_ATTEMPTS:
            attempt += 1
            await self._adaptive_gate.acquire()
            timeout = self._timeout_estimator.estimate()
            call_started = self._time()
            try:
                async with asyncio.timeout(timeout):
                    result = await fn(*args, **kwargs)
            except BaseException as exc:
                last_exc = exc
                # ResilienceError propagates first, never retried.
                if isinstance(exc, ResilienceError):
                    give_up_reason = _GIVE_UP_NON_RETRY
                    break
                # BaseException outside Exception (CancelledError,
                # KeyboardInterrupt, SystemExit) propagates immediately.
                if not isinstance(exc, Exception):
                    raise
                # Non-retryable Exception: surface unchanged.
                if not isinstance(exc, state.effective_timeout_errors):
                    give_up_reason = _GIVE_UP_NON_RETRY
                    break
                # Retryable slow-down: shrink CUBIC, try the budget.
                self._adaptive_gate.on_slow_down()
                if attempt >= _MAX_ATTEMPTS:
                    give_up_reason = _GIVE_UP_ATTEMPTS
                    break
                allowed = await self._retry_budget.try_acquire()
                if not allowed:
                    logger.debug(
                        "shield %s: retry budget exhausted", self._name
                    )
                    give_up_reason = _GIVE_UP_BUDGET
                    break
                retries_consumed += 1
                delay = self._backoff_for(attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            else:
                latency = self._time() - call_started
                self._timeout_estimator.record(latency)
                self._adaptive_gate.on_success()
                if retries_consumed == 0:
                    await self._retry_budget.refund(1)
                else:
                    await self._retry_budget.refund(retries_consumed)
                if state.config.cache is not None:
                    self._fire_and_forget_cache_set(args, kwargs, result)
                return result
        # Give-up path: try cache, then fallback, then re-raise with note.
        elapsed = self._time() - started
        note = (
            f"shield: {give_up_reason} after {attempt}/{_MAX_ATTEMPTS} "
            f"attempts in {elapsed:.2f}s ({state.config.profile_name} profile)"
        )
        return await self._handle_give_up(state, args, kwargs, last_exc, note)

    def _backoff_for(self, attempt_number: int) -> float:
        """Return the sleep delay before retry `attempt_number`."""
        # `attempt_number` is the just-failed attempt index (1..N). The
        # retry index `i` used by the formula starts at 1 for the first
        # retry, which is the same value.
        config = self._state.config
        cap = config.backoff_cap
        scale = config.backoff_scale
        ceiling = min(scale * (2 ** (attempt_number - 1)), cap)
        return self._random() * ceiling

    async def _handle_give_up(
        self,
        state: _State,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        exc: BaseException | None,
        note: str,
    ) -> Any:  # noqa: ANN401
        # 1. Cache lookup.
        config = state.config
        cache = config.cache
        if cache is not None:
            key = self._compute_key(args, kwargs)
            try:
                value = await cache.get(key)
            except Exception:  # noqa: BLE001
                logger.debug("shield: cache read failed", exc_info=True)
                value = None
            if value is not None:
                return value
        # 2. Fallback callable.
        if config.fallback is not None and exc is not None:
            return await _maybe_await(config.fallback(exc))
        # 3. Re-raise with PEP 678 note.
        if exc is None:  # pragma: no cover  # defensive
            msg = "Shield give-up without an exception"
            raise RuntimeError(msg)
        exc.add_note(note)
        raise exc

    def _compute_key(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> str:
        """Compute the cache key for one call."""
        custom = self._state.config.cache_key
        if custom is not None:
            return custom(*args, **kwargs)
        return default_cache_key(self._name, args, kwargs)

    def _fire_and_forget_cache_set(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        value: Any,  # noqa: ANN401
    ) -> None:
        """Write the successful return value to the cache without awaiting."""
        cache = self._state.config.cache
        if cache is None:  # pragma: no cover
            return
        key = self._compute_key(args, kwargs)
        task = asyncio.create_task(_safe_cache_set(cache, key, value))
        # Hold a strong reference and silence "task was destroyed" warnings.
        task.add_done_callback(_discard_task)

    async def _apply_reconfigure(self, new_config: _BaseShieldConfig) -> None:
        """Rebuild derived state from `new_config`."""
        # Live reconfigure swaps the snapshot. In-flight `_execute`
        # loops keep the previous snapshot through `state`.
        self._retry_budget = _RetryBudget(
            capacity=new_config.max_consecutive_failures
        )
        cap = new_config.max_rate or new_config.max_rate_cap_default
        self._adaptive_gate = _AdaptiveGate(
            initial_max_rate=new_config.initial_max_rate,
            capacity=new_config.adaptive_burst_capacity,
            min_rate_floor=new_config.min_rate_floor,
            max_rate_cap=cap,
            time_source=self._time,
        )
        self._timeout_estimator = _TimeoutEstimator(
            initial_timeout=new_config.initial_timeout,
            clamp_min=new_config.timeout_clamp_min,
            clamp_max=new_config.timeout_clamp_max,
        )
        self._state = _State(
            config=new_config,
            effective_timeout_errors=new_config.effective_timeout_errors(),
        )


def _discard_task(task: asyncio.Task[Any]) -> None:
    """Swallow the cache-write task result for fire-and-forget semantics."""
    import contextlib  # noqa: PLC0415

    with contextlib.suppress(Exception):  # pragma: no cover
        task.result()


def _is_async_callable(fn: object) -> bool:
    """Return True when `fn` is callable and returns an awaitable.

    Covers `functools.partial`-wrapped coroutines that
    `iscoroutinefunction` does not recognise.
    """
    target = fn.func if isinstance(fn, functools.partial) else fn
    return inspect.iscoroutinefunction(target)
