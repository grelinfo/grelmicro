"""Shield resilience pattern."""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger
from typing import Annotated, Any, Self, TypeVar

from pydantic import Discriminator, PositiveFloat, ValidationError
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    env_load_default,
    env_prefixes,
    warn_ignored_env,
)
from grelmicro.clock import monotonic, sleep
from grelmicro.errors import SettingsValidationError
from grelmicro.metrics import _emit
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
    """Return the profile name from env, defaulting to `api`.

    Falls back from the instance address to the kind address, which is the
    order R5 applies to every other value.
    """
    instance_prefix, kind_prefix = env_prefixes("SHIELD", name)
    env_key = f"{instance_prefix}PROFILE"
    value = os.environ.get(env_key, "").strip().lower()
    if not value and kind_prefix:
        env_key = f"{kind_prefix}PROFILE"
        value = os.environ.get(env_key, "").strip().lower()
    if value and value not in _PROFILE_BY_NAME:
        msg = (
            f"{env_key} is not a valid profile. "
            f"Expected one of: internal, api, slow."
        )
        raise SettingsValidationError(msg)
    return value or "api"


def _validate(
    cls: type[_BaseShieldConfig], kwargs: dict[str, Any]
) -> _BaseShieldConfig:
    """Build `cls`, raising `SettingsValidationError` on a bad value.

    `Shield` resolves its own configuration, so it wraps the pydantic
    error here instead of through `resolve_config`. Without this the raw
    error escapes carrying `input_value`, which is the rejected value.
    """
    try:
        return cls.model_validate(kwargs)
    except ValidationError as error:
        raise SettingsValidationError(error) from None


def _fill_from_env(
    kwargs: dict[str, Any],
    field: str,
    passed: Any,  # noqa: ANN401
    read: Callable[[str], str | None],
    *,
    cast: Callable[[str], Any] | None = None,
) -> None:
    """Take the caller's value, else the environment's, else leave unset.

    A keyword beats the environment, which is the same order
    `resolve_config` applies for every other pattern.

    A value the cast refuses raises `SettingsValidationError` naming the
    variable. `float()` reports the rejected string in its own message,
    so it is never allowed to escape.
    """
    if passed is not None:
        kwargs[field] = passed
        return
    value = read(field.upper())
    if value is None or (cast is not None and value.strip() == ""):
        return
    if cast is None:
        kwargs[field] = value
        return
    try:
        kwargs[field] = cast(value)
    except ValueError:
        msg = f"{field}: input is not a valid number"
        raise SettingsValidationError(msg) from None


def _resolve_config_from_env(
    name: str,
    *,
    profile: str | None,
    timeout_errors: Any,  # noqa: ANN401
    max_rate: float | None,
    cache: Any,  # noqa: ANN401
    cache_key: Callable[..., str] | None,
    fallback: Callable[..., Any] | None,
) -> _BaseShieldConfig:
    """Build a `_BaseShieldConfig` reading defaults from environment variables.

    `profile` is the preset the calling door pinned. Only the bare
    constructor leaves it `None`, and only then is `GREL_SHIELD_{NAME}_PROFILE`
    consulted. A factory names the preset in code, so the variable cannot
    override it, which is the ordinary rule that a keyword beats the
    environment.
    """
    if profile is None:
        profile = _load_profile_from_env(name)
    cls = _PROFILE_BY_NAME[profile]
    env_prefix, kind_prefix = env_prefixes("SHIELD", name)

    def _env(field: str) -> str | None:
        """Read the instance variable, falling back to the kind-wide one."""
        value = os.environ.get(f"{env_prefix}{field}")
        if value is None and kind_prefix:
            value = os.environ.get(f"{kind_prefix}{field}")
        return value

    kwargs: dict[str, Any] = {"kind": profile}
    _fill_from_env(kwargs, "timeout_errors", timeout_errors, _env)
    _fill_from_env(kwargs, "max_rate", max_rate, _env, cast=float)
    if cache is not None:
        kwargs["cache"] = cache
    if cache_key is not None:
        kwargs["cache_key"] = cache_key
    if fallback is not None:
        kwargs["fallback"] = fallback
    return _validate(cls, kwargs)


def _build_config(
    name: str,
    *,
    profile: str,
    pinned_profile: str | None = None,
    timeout_errors: Any,  # noqa: ANN401
    max_rate: float | None,
    cache: Any,  # noqa: ANN401
    cache_key: Callable[..., str] | None,
    fallback: Callable[..., Any] | None,
    env_load: bool | None,
) -> _BaseShieldConfig:
    """Resolve a `_BaseShieldConfig` from kwargs or env."""
    implicit = env_load is None
    if implicit:
        env_load = env_load_default()
    if env_load:
        return _resolve_config_from_env(
            name,
            profile=pinned_profile,
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
    if implicit:
        # R7: a variable that would have applied is never dropped in
        # silence. Shield resolves its own configuration, so it reports
        # through the same helper every other pattern reaches via
        # `resolve_config`.
        instance_prefix, kind_prefix = env_prefixes("SHIELD", name)
        warn_ignored_env(
            cls,
            instance_prefix,
            kwargs,
            kind_env_prefix=kind_prefix,
        )
    return _validate(cls, kwargs)


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot of the published Shield configuration and helpers.

    Each in-flight call captures one `_State` reference at the top of
    `_execute`. A subsequent `reconfigure` swaps `Shield._state` to a
    fresh instance with new helpers; the in-flight call continues to
    use its captured snapshot end-to-end.
    """

    config: _BaseShieldConfig
    effective_timeout_errors: tuple[type[BaseException], ...]
    retry_budget: _RetryBudget
    adaptive_gate: _AdaptiveGate
    timeout_estimator: _TimeoutEstimator


async def _maybe_await(value: Any) -> Any:  # noqa: ANN401
    """Await `value` when it is a coroutine, return it as-is otherwise."""
    if inspect.isawaitable(value):
        return await value
    return value


_CACHE_ERROR_LOG_INTERVAL = 60.0
"""Seconds between two cache-failure warnings from one shield.

A cache write rides along with every successful call, so an unreachable
store would otherwise log once per request for as long as it stays down.
The counter carries the volume, the log carries the diagnosis.
"""


async def _safe_cache_set(
    cache: Any,  # noqa: ANN401
    key: str,
    value: Any,  # noqa: ANN401
    report: Callable[[str, str], None],
) -> None:
    """Write `value` to `cache` under `key`.

    A failed write never breaks the call it rode along with, but it is
    reported: this is the copy the shield serves when the primary fails,
    so a store that silently stops accepting writes leaves nothing to
    fall back to. `BaseException` (notably `asyncio.CancelledError`)
    propagates so task cancellation during shutdown reaches the asyncio
    scheduler unchanged.
    """
    try:
        await cache.set(key, value)
    except Exception as error:  # noqa: BLE001 - reported, never propagated
        _emit.incr(
            "grelmicro.shield.cache_writes",
            outcome="error",
            **{"error.type": type(error).__name__},
        )
        report(key, type(error).__name__)
    else:
        _emit.incr("grelmicro.shield.cache_writes", outcome="success")


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
                "process-wide `GREL_ENV_LOAD` flag. "
                "Pass False when the values here are the whole "
                "truth, because env reads fill every field not passed."
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
            register=True,
        )

    def _setup(
        self,
        *,
        name: str,
        config: _BaseShieldConfig,
        time_source: Callable[[], float] | None,
        random_source: Callable[[], float] | None,
        register: bool = False,
    ) -> None:
        """Wire the resolved config and helpers onto the instance.

        Registers the instance for external reload under its
        name-as-namespace prefix when `register` is true. The declarative
        `from_config` path passes `register=False` and stays static.
        """
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        if register:
            self._track_reconfigure(default_env_prefix("SHIELD", name))
        self._time = time_source or monotonic
        self._random = random_source or random.random
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._cache_error_logged_at: float | None = None
        self._state = self._build_state(config)

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
        env_load: bool | None = None,
    ) -> Self:
        """Build a Shield with the preset pinned by the calling factory.

        Values still resolve from the environment when the gate is on, the
        same way every other pattern's factory behaves. The preset is code's
        to choose, so `GREL_SHIELD_{NAME}_PROFILE` cannot override it here.
        """
        config = _build_config(
            name,
            profile=profile,
            pinned_profile=profile,
            timeout_errors=timeout_errors,
            max_rate=max_rate,
            cache=cache,
            cache_key=cache_key,
            fallback=fallback,
            env_load=env_load,
        )
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            name=name,
            config=config,
            time_source=None,
            random_source=None,
            register=True,
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

    def __call__[**P, R](
        self,
        fn: Annotated[
            Callable[P, Awaitable[R]],
            Doc("Async function to decorate."),
        ],
    ) -> Callable[P, Awaitable[R]]:
        """Decorate `fn` so each call runs through this Shield."""
        if not inspect.iscoroutinefunction(fn):
            msg = (
                "Shield only decorates async functions. "
                f"Got {fn!r}. Wrap sync code in asyncio.to_thread(...)."
            )
            raise TypeError(msg)

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
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
        while attempt < _MAX_ATTEMPTS:  # pragma: no branch
            attempt += 1
            await state.adaptive_gate.acquire()
            timeout = state.timeout_estimator.estimate()
            call_started = self._time()
            try:
                async with asyncio.timeout(timeout):
                    result = await fn(*args, **kwargs)
            except BaseException as exc:
                # ResilienceError propagates immediately. No retry, no
                # CUBIC update, no cache or fallback recovery, no PEP
                # 678 note. Matches the BaseException-outside-Exception
                # treatment in the classification table.
                if isinstance(exc, ResilienceError):
                    raise
                # BaseException outside Exception (CancelledError,
                # KeyboardInterrupt, SystemExit) propagates immediately.
                if not isinstance(exc, Exception):
                    raise
                last_exc = exc
                # Non-retryable Exception: surface unchanged.
                if not isinstance(exc, state.effective_timeout_errors):
                    give_up_reason = _GIVE_UP_NON_RETRY
                    break
                # Retryable slow-down: shrink CUBIC, try the budget.
                state.adaptive_gate.on_slow_down()
                if attempt >= _MAX_ATTEMPTS:
                    give_up_reason = _GIVE_UP_ATTEMPTS
                    break
                allowed = await state.retry_budget.try_acquire()
                if not allowed:
                    logger.debug(
                        "shield %s: retry budget exhausted", self._name
                    )
                    give_up_reason = _GIVE_UP_BUDGET
                    break
                retries_consumed += 1
                delay = self._backoff_for(state, attempt)
                if delay > 0:  # pragma: no branch
                    await sleep(delay)
                continue
            else:
                latency = self._time() - call_started
                state.timeout_estimator.record(latency)
                state.adaptive_gate.on_success()
                if retries_consumed == 0:
                    await state.retry_budget.refund(1)
                else:
                    await state.retry_budget.refund(retries_consumed)
                if state.config.cache is not None:
                    self._fire_and_forget_cache_set(state, args, kwargs, result)
                return result
        # Give-up path: try cache, then fallback, then re-raise with note.
        elapsed = self._time() - started
        note = (
            f"shield: {give_up_reason} after {attempt}/{_MAX_ATTEMPTS} "
            f"attempts in {elapsed:.2f}s ({state.config.profile_name} profile)"
        )
        return await self._handle_give_up(state, args, kwargs, last_exc, note)

    def _backoff_for(self, state: _State, attempt_number: int) -> float:
        """Return the sleep delay before retry `attempt_number`."""
        # `attempt_number` is the just-failed attempt index (1..N). The
        # retry index `i` used by the formula starts at 1 for the first
        # retry, which is the same value.
        config = state.config
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
            key = self._compute_key(state, args, kwargs)
            try:
                value = await cache.get(key)
            except Exception as error:  # noqa: BLE001 - reported, never propagated
                # Same broken-cache condition the write path reports, and it
                # matters more here: this is the give-up path, so a silent
                # read failure means no fallback at the moment it is needed.
                self._report_cache_error(key, type(error).__name__)
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
        self,
        state: _State,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> str:
        """Compute the cache key for one call."""
        custom = state.config.cache_key
        if custom is not None:
            return custom(*args, **kwargs)
        return default_cache_key(self._name, args, kwargs)

    def _fire_and_forget_cache_set(
        self,
        state: _State,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        value: Any,  # noqa: ANN401
    ) -> None:
        """Write the successful return value to the cache without awaiting.

        The task is tracked in `self._pending_tasks` so the event loop
        retains a strong reference and cannot garbage-collect the task
        mid-execution. The done callback removes the entry.
        """
        cache = state.config.cache
        if cache is None:  # pragma: no cover
            return
        key = self._compute_key(state, args, kwargs)
        task = asyncio.create_task(
            _safe_cache_set(cache, key, value, self._report_cache_error)
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _report_cache_error(self, key: str, error_type: str) -> None:
        """Warn about a failing cache, at most once per interval.

        The counter records every failure. This log exists to name the key
        and the error once, not once per request.
        """
        now = self._time()
        last = self._cache_error_logged_at
        if last is not None and (now - last) < _CACHE_ERROR_LOG_INTERVAL:
            return
        self._cache_error_logged_at = now
        logger.warning(
            "Shield %r cache write failed for key %r with %s, there will be "
            "no fallback copy to serve. Further failures are counted on "
            "grelmicro.shield.cache_writes and logged at most every %.0fs.",
            self.name,
            key,
            error_type,
            _CACHE_ERROR_LOG_INTERVAL,
        )

    async def _apply_reconfigure(self, new_config: _BaseShieldConfig) -> None:
        """Rebuild derived state from `new_config`.

        Swaps `self._state` to a fresh `_State` carrying new helpers.
        In-flight calls keep their captured snapshot end-to-end.
        """
        self._state = self._build_state(new_config)

    def _build_state(self, config: _BaseShieldConfig) -> _State:
        """Build a fresh `_State` with helpers wired to `config`."""
        cap = config.max_rate or config.max_rate_cap_default
        return _State(
            config=config,
            effective_timeout_errors=config.effective_timeout_errors(),
            retry_budget=_RetryBudget(capacity=config.max_consecutive_failures),
            adaptive_gate=_AdaptiveGate(
                initial_max_rate=config.initial_max_rate,
                capacity=config.adaptive_burst_capacity,
                min_rate_floor=config.min_rate_floor,
                max_rate_cap=cap,
                time_source=self._time,
            ),
            timeout_estimator=_TimeoutEstimator(
                initial_timeout=config.initial_timeout,
                clamp_min=config.timeout_clamp_min,
                clamp_max=config.timeout_clamp_max,
            ),
        )


def _is_async_callable(fn: object) -> bool:
    """Return True when `fn` is callable and returns an awaitable.

    Covers `functools.partial`-wrapped coroutines that
    `iscoroutinefunction` does not recognise.
    """
    target = fn.func if isinstance(fn, functools.partial) else fn
    return inspect.iscoroutinefunction(target)
