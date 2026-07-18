"""Retry."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from importlib import import_module
from inspect import iscoroutinefunction
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Self,
    TypeVar,
    overload,
)

if TYPE_CHECKING:
    from types import TracebackType

from pydantic import (
    BaseModel,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
)
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    parse_csv_or_json,
    resolve_config,
)
from grelmicro.clock import monotonic as clock_monotonic
from grelmicro.clock import sleep as clock_sleep
from grelmicro.metrics import _emit
from grelmicro.resilience._match import Match, Matcher
from grelmicro.resilience._outcome import Outcome
from grelmicro.resilience._retry_strategy import build_retry_strategy
from grelmicro.resilience.backoffs import (
    ConstantBackoff,
    ExponentialBackoff,
    RetryBackoffConfig,
)

__all__ = [
    "Retry",
    "RetryAttempt",
    "RetryConfig",
    "retry",
    "retrying",
]

logger = getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

WhenInput = (
    Match
    | type[Exception]
    | tuple[type[Exception], ...]
    | Callable[[Exception], bool]
)
"""User-facing shape accepted by ``when=``.

A [`Match`][grelmicro.resilience.Match] instance, or one of the
shorthand forms a Match would build for you: a single exception
class, a tuple of classes, or a callable predicate on the
exception. Bare shapes are coerced to ``Match.exception(...)``.
"""


def _coerce_to_match(value: Any) -> Match:  # noqa: ANN401
    """Coerce a non-Match shorthand into a ``Match`` instance.

    The validator on ``RetryConfig.when`` short-circuits Match
    instances before calling this helper, so the input here is one
    of the shorthand forms (class, tuple, callable, FQN env list).
    """
    if isinstance(value, type) and issubclass(value, Exception):
        return Match.exception(value)
    if isinstance(value, tuple) and all(
        isinstance(t, type) and issubclass(t, Exception) for t in value
    ):
        return Match.exception(*value)
    if callable(value):
        return Match.exception(value)
    msg = (
        "when= must be a Match, an Exception class, a tuple of "
        f"Exception classes, or a callable. Got {value!r}"
    )
    raise TypeError(msg)


def _resolve_fqn(fqn: str) -> type[Exception]:
    """Resolve a fully-qualified name to an Exception class."""
    module_path, _, name = fqn.rpartition(".")
    if not module_path:
        msg = f"when= env entry must be a fully-qualified name, got {fqn!r}"
        raise ValueError(msg)
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        msg = (
            f"when= env entry {fqn!r}: cannot import module "
            f"{module_path!r} ({exc})"
        )
        raise ValueError(msg) from exc
    try:
        cls = getattr(module, name)
    except AttributeError as exc:
        msg = (
            f"when= env entry {fqn!r}: module {module_path!r} has no "
            f"attribute {name!r}"
        )
        raise ValueError(msg) from exc
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        msg = f"when= env entry {fqn!r} is not an Exception subclass"
        raise TypeError(msg)
    return cls


class RetryConfig(BaseModel, frozen=True, extra="forbid"):
    """Retry policy configuration.

    Holds the top-level retry fields plus a discriminated backoff
    sub-config. Frozen Pydantic data class. Three-paths
    configuration: kwargs, instance, or env vars.

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    attempts: Annotated[
        PositiveInt,
        Doc(
            "Total calls including the first. ``attempts=1`` means "
            "no retry. The default ``3`` allows two retries."
        ),
    ] = 3

    max_seconds: Annotated[
        PositiveFloat | None,
        Doc(
            "Optional wall-clock budget in seconds, measured from the "
            "first attempt. Retrying stops as soon as either ``attempts`` "
            "is reached or this budget elapses, whichever comes first. The "
            "budget is checked between attempts, so one backoff may run "
            "slightly past it. ``None`` (default) means no time limit."
        ),
    ] = None

    when: Annotated[
        Match,
        Doc(
            "Outcome filter that engages the retry. Pass a "
            "[`Match`][grelmicro.resilience.Match] (e.g. "
            "``Match.exception(httpx.HTTPError) | Match.result(None)``) "
            "or a shorthand: an exception class, a tuple of classes, "
            "or a predicate on the exception. ``BaseException`` "
            "subclasses outside ``Exception`` are never retried."
        ),
    ]

    backoff: Annotated[
        RetryBackoffConfig,
        Field(default_factory=ExponentialBackoff),
        Doc(
            "Backoff algorithm config. Discriminated union over "
            "[`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff], "
            "[`ConstantBackoff`][grelmicro.resilience.ConstantBackoff], "
            "[`LinearBackoff`][grelmicro.resilience.LinearBackoff], "
            "[`FibonacciBackoff`][grelmicro.resilience.FibonacciBackoff], "
            "and [`RandomBackoff`][grelmicro.resilience.RandomBackoff]. "
            "Default: exponential with full jitter."
        ),
    ]

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("when", mode="before")
    @classmethod
    def _coerce_when(cls, value: Any) -> Any:  # noqa: ANN401
        """Coerce shorthand shapes (and the env string) to a ``Match``.

        Accepts a ``Match`` directly, an exception class, a tuple of
        classes, a callable predicate on the exception, or a
        CSV/JSON env string of FQNs (e.g. ``"httpx.HTTPError"``).
        """
        if isinstance(value, Match):
            return value
        # Env path: a CSV or JSON string of FQNs.
        if isinstance(value, str):
            value = parse_csv_or_json(value)
        # List/tuple of items (FQN strings or resolved classes).
        if isinstance(value, list | tuple) and not (
            isinstance(value, tuple)
            and all(
                isinstance(item, type) and issubclass(item, Exception)
                for item in value
            )
        ):
            resolved: tuple[type[Exception], ...] = tuple(
                _resolve_fqn(item) if isinstance(item, str) else item
                for item in value
            )
            return Match.exception(*resolved)
        return _coerce_to_match(value)


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its compiled matcher.

    A fresh ``RetryStrategy`` is built per loop from
    ``state.config.backoff`` (strategies are stateful for jitter).
    The matcher is bound once per snapshot so the hot path is a
    single callable invocation. Reconfigure swaps the snapshot
    atomically.
    """

    config: RetryConfig
    matcher: Matcher


class RetryAttempt:
    """One iteration of a retry loop.

    Yielded by [`Retry`][grelmicro.resilience.Retry] iteration and
    by [`retrying`][grelmicro.resilience.retrying]. Used as an
    async (or sync) context manager. Suppresses retryable
    exceptions until attempts are exhausted, then re-raises the
    underlying error with a PEP 678 note.

    The block form sees only exceptions, not return values. Use the
    decorator form ([`@retry`][grelmicro.resilience.retry] or
    ``policy(fn)``) for result-based retry.
    """

    __slots__ = (
        "_attempts",
        "_loop",
        "_matcher",
        "_max_seconds",
        "_started_at",
        "delay_before",
        "number",
    )

    def __init__(
        self,
        *,
        number: int,
        delay_before: float,
        attempts: int,
        matcher: Matcher,
        loop: _AttemptLoop,
        started_at: float,
        max_seconds: float | None,
    ) -> None:
        """Initialize one retry attempt."""
        self.number = number
        """1-indexed attempt number. The first call is ``1``."""

        self.delay_before = delay_before
        """Seconds slept before this attempt (``0.0`` for the first)."""

        self._attempts = attempts
        self._matcher = matcher
        self._loop = loop
        self._started_at = started_at
        self._max_seconds = max_seconds

    async def __aenter__(self) -> Self:
        """Enter the attempt context."""
        return self

    def __enter__(self) -> Self:
        """Enter the attempt context (sync)."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Suppress retryable exceptions, re-raise on exhaustion."""
        return self._handle_exit(exc)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Suppress retryable exceptions, re-raise on exhaustion (sync)."""
        return self._handle_exit(exc)

    def _handle_exit(self, exc: BaseException | None) -> bool:
        if exc is None:
            self._loop.stop()
            return False
        # Never retry cooperative-cancellation or shutdown signals,
        # regardless of the user's filter. Letting these propagate is
        # required for correct asyncio shutdown.
        if not isinstance(exc, Exception):
            return False
        if not self._matcher(Outcome.from_exception(exc)):
            return False
        elapsed = clock_monotonic() - self._started_at
        attempts_done = self.number >= self._attempts
        time_done = (
            self._max_seconds is not None and elapsed >= self._max_seconds
        )
        if attempts_done or time_done:
            backoff_name = self._loop.backoff_name
            if attempts_done:
                exc.add_note(
                    f"retry: {self._attempts}/{self._attempts} attempts "
                    f"exhausted in {elapsed:.2f}s ({backoff_name} backoff)"
                )
            else:
                exc.add_note(
                    f"retry: {self._max_seconds}s budget elapsed after "
                    f"{self.number} attempt(s) in {elapsed:.2f}s "
                    f"({backoff_name} backoff)"
                )
            return False
        return True


class _AttemptLoop:
    """Shared mutable state across the iterator and its yielded attempts."""

    __slots__ = ("_done", "backoff_name")

    def __init__(self, backoff_name: str) -> None:
        self._done = False
        self.backoff_name = backoff_name

    @property
    def done(self) -> bool:
        return self._done

    def stop(self) -> None:
        self._done = True


def _backoff_name(config: RetryBackoffConfig) -> str:
    """Return a human-readable name for the backoff config."""
    return config.kind


async def _async_iter(
    config: RetryConfig,
    matcher: Matcher,
) -> AsyncIterator[RetryAttempt]:
    """Yield successive ``RetryAttempt`` objects, sleeping between attempts."""
    strategy = build_retry_strategy(config.backoff)
    loop = _AttemptLoop(_backoff_name(config.backoff))
    started_at = clock_monotonic()
    delay_before = 0.0
    for number in range(1, config.attempts + 1):  # pragma: no branch
        if delay_before > 0:
            await clock_sleep(delay_before)
        yield RetryAttempt(
            number=number,
            delay_before=delay_before,
            attempts=config.attempts,
            matcher=matcher,
            loop=loop,
            started_at=started_at,
            max_seconds=config.max_seconds,
        )
        if loop.done:
            return
        delay_before = strategy.delay(number)


def _sync_iter(
    config: RetryConfig,
    matcher: Matcher,
) -> Iterator[RetryAttempt]:
    """Yield successive ``RetryAttempt`` objects, sleeping between attempts."""
    strategy = build_retry_strategy(config.backoff)
    loop = _AttemptLoop(_backoff_name(config.backoff))
    started_at = clock_monotonic()
    delay_before = 0.0
    for number in range(1, config.attempts + 1):  # pragma: no branch
        if delay_before > 0:
            time.sleep(delay_before)
        yield RetryAttempt(
            number=number,
            delay_before=delay_before,
            attempts=config.attempts,
            matcher=matcher,
            loop=loop,
            started_at=started_at,
            max_seconds=config.max_seconds,
        )
        if loop.done:
            return
        delay_before = strategy.delay(number)


def _emit_retry(name: str, *, started_at: float, outcome: str) -> None:
    """Emit retry attempts and duration metrics for one run.

    `grelmicro.retry.attempts` counts each run with a bounded ``outcome``
    (``success`` or ``error``) and the policy ``retry.name``.
    `grelmicro.retry.duration` records the total run time in seconds. Both
    are no-ops when no `Metrics` component is active.
    """
    _emit.incr(
        "grelmicro.retry.attempts",
        **{"retry.name": name, "outcome": outcome},
    )
    _emit.record_duration(
        "grelmicro.retry.duration",
        clock_monotonic() - started_at,
        **{"retry.name": name},
    )


async def _run_async(
    fn: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    config: RetryConfig,
    matcher: Matcher,
    name: str = "anonymous",
) -> Any:  # noqa: ANN401
    """Decorator/class-form async wrapper. Handles exception and result retries."""
    strategy = build_retry_strategy(config.backoff)
    started_at = clock_monotonic()
    backoff_name = _backoff_name(config.backoff)
    delay = 0.0
    last_result: Any = None
    for number in range(1, config.attempts + 1):
        if delay > 0:
            await clock_sleep(delay)
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            if not matcher(Outcome.from_exception(exc)):
                raise
            elapsed = clock_monotonic() - started_at
            attempts_done = number >= config.attempts
            time_done = (
                config.max_seconds is not None and elapsed >= config.max_seconds
            )
            if attempts_done or time_done:
                if attempts_done:
                    exc.add_note(
                        f"retry: {config.attempts}/{config.attempts} "
                        f"attempts exhausted in {elapsed:.2f}s "
                        f"({backoff_name} backoff)"
                    )
                else:
                    exc.add_note(
                        f"retry: {config.max_seconds}s budget elapsed "
                        f"after {number} attempt(s) in {elapsed:.2f}s "
                        f"({backoff_name} backoff)"
                    )
                _emit_retry(name, started_at=started_at, outcome="error")
                raise
            delay = strategy.delay(number)
            continue
        if not matcher(Outcome.from_result(result)):
            _emit_retry(name, started_at=started_at, outcome="success")
            return result
        last_result = result
        if number >= config.attempts or (
            config.max_seconds is not None
            and clock_monotonic() - started_at >= config.max_seconds
        ):
            _emit_retry(name, started_at=started_at, outcome="error")
            return last_result
        delay = strategy.delay(number)
    return last_result  # pragma: no cover  # unreachable


def _run_sync(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    config: RetryConfig,
    matcher: Matcher,
    name: str = "anonymous",
) -> Any:  # noqa: ANN401
    """Decorator/class-form sync wrapper. Handles exception and result retries."""
    strategy = build_retry_strategy(config.backoff)
    started_at = clock_monotonic()
    backoff_name = _backoff_name(config.backoff)
    delay = 0.0
    last_result: Any = None
    for number in range(1, config.attempts + 1):
        if delay > 0:
            time.sleep(delay)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if not matcher(Outcome.from_exception(exc)):
                raise
            elapsed = clock_monotonic() - started_at
            attempts_done = number >= config.attempts
            time_done = (
                config.max_seconds is not None and elapsed >= config.max_seconds
            )
            if attempts_done or time_done:
                if attempts_done:
                    exc.add_note(
                        f"retry: {config.attempts}/{config.attempts} "
                        f"attempts exhausted in {elapsed:.2f}s "
                        f"({backoff_name} backoff)"
                    )
                else:
                    exc.add_note(
                        f"retry: {config.max_seconds}s budget elapsed "
                        f"after {number} attempt(s) in {elapsed:.2f}s "
                        f"({backoff_name} backoff)"
                    )
                _emit_retry(name, started_at=started_at, outcome="error")
                raise
            delay = strategy.delay(number)
            continue
        if not matcher(Outcome.from_result(result)):
            _emit_retry(name, started_at=started_at, outcome="success")
            return result
        last_result = result
        if number >= config.attempts or (
            config.max_seconds is not None
            and clock_monotonic() - started_at >= config.max_seconds
        ):
            _emit_retry(name, started_at=started_at, outcome="error")
            return last_result
        delay = strategy.delay(number)
    return last_result  # pragma: no cover  # unreachable


class Retry(Reconfigurable[RetryConfig]):
    """Retry policy.

    A named, reusable retry policy with three-paths configuration
    and live reconfiguration. Use the
    [`Retry.exponential`][grelmicro.resilience.Retry.exponential]
    or [`Retry.constant`][grelmicro.resilience.Retry.constant]
    factory classmethods for the common case. Pass a pre-built
    backoff config to the constructor when configuration is
    assembled elsewhere.

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The name of the retry policy. Used as the env "
                "namespace and exposed via the ``name`` property."
            ),
        ],
        backoff: Annotated[
            RetryBackoffConfig | None,
            Doc(
                "The backoff algorithm config. Pass any "
                "[`RetryBackoffConfig`][grelmicro.resilience.RetryBackoffConfig] "
                "variant or omit for the default exponential + full jitter."
            ),
        ] = None,
        *,
        when: Annotated[
            WhenInput | None,
            Doc(
                "Outcome filter that engages the retry. Pass a "
                "[`Match`][grelmicro.resilience.Match] or one of the "
                "shorthand forms (exception class, tuple, callable). "
                "Required unless ``config=`` is given."
            ),
        ] = None,
        attempts: Annotated[
            PositiveInt | None,
            Doc("Total calls including the first. Default ``3``."),
        ] = None,
        max_seconds: Annotated[
            PositiveFloat | None,
            Doc(
                "Optional wall-clock budget in seconds. Retrying stops "
                "when either ``attempts`` or this budget is reached, "
                "whichever comes first. Default no time limit."
            ),
        ] = None,
        config: Annotated[
            RetryConfig | None,
            Doc(
                "A pre-built [`RetryConfig`][grelmicro.resilience.RetryConfig]. "
                "Mutually exclusive with the per-field kwargs."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults "
                "to the process-wide ``GREL_ENV_LOAD`` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the retry policy."""
        self._name = name
        env_prefix = default_env_prefix("RETRY", name)
        kwargs: dict[str, object | None] = {
            "attempts": attempts,
            "max_seconds": max_seconds,
            "when": when,
            "backoff": backoff,
        }
        resolved = resolve_config(
            RetryConfig,
            explicit=config,
            kwargs=kwargs,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        self._config = resolved
        self._state = _State(config=resolved, matcher=resolved.when)
        self._reconfigure_lock = asyncio.Lock()
        if config is None:
            self._track_reconfigure(env_prefix)

    @property
    def name(self) -> str:
        """Return the retry policy identity."""
        return self._name

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc("The name of the retry policy."),
        ],
        config: Annotated[
            RetryConfig,
            Doc(
                """
                The pre-built retry configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree. The environment path is
                bypassed and the config is used as-is.
                """
            ),
        ],
    ) -> Self:
        """Construct a `Retry` from a name and a pre-built `RetryConfig`."""
        return cls(name, config=config)

    @classmethod
    def exponential(
        cls,
        name: Annotated[str, Doc("The name of the retry policy.")],
        *,
        when: Annotated[
            WhenInput,
            Doc("Outcome filter that engages the retry."),
        ],
        attempts: Annotated[
            PositiveInt,
            Doc("Total calls including the first."),
        ] = 3,
        max_seconds: Annotated[
            PositiveFloat | None,
            Doc(
                "Optional wall-clock budget in seconds, whichever comes "
                "first with ``attempts``."
            ),
        ] = None,
        base_delay: Annotated[
            PositiveFloat,
            Doc("Initial delay in seconds before the first retry."),
        ] = 0.1,
        max_delay: Annotated[
            PositiveFloat,
            Doc("Maximum delay in seconds. Caps exponential growth."),
        ] = 30.0,
        jitter: Annotated[
            Literal["none", "full", "decorrelated"],
            Doc("Jitter mode."),
        ] = "full",
        env_load: Annotated[
            bool | None,
            Doc("Whether to read environment variables."),
        ] = None,
    ) -> Self:
        """Construct a retry policy with exponential backoff.

        Convenience factory for the common case. Builds an
        [`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff]
        and forwards to the constructor.
        """
        backoff = ExponentialBackoff(
            base_delay=base_delay, max_delay=max_delay, jitter=jitter
        )
        return cls(
            name,
            backoff,
            when=when,
            attempts=attempts,
            max_seconds=max_seconds,
            env_load=env_load,
        )

    @classmethod
    def constant(
        cls,
        name: Annotated[str, Doc("The name of the retry policy.")],
        *,
        when: Annotated[
            WhenInput,
            Doc("Outcome filter that engages the retry."),
        ],
        attempts: Annotated[
            PositiveInt,
            Doc("Total calls including the first."),
        ] = 3,
        max_seconds: Annotated[
            PositiveFloat | None,
            Doc(
                "Optional wall-clock budget in seconds, whichever comes "
                "first with ``attempts``."
            ),
        ] = None,
        delay: Annotated[
            PositiveFloat,
            Doc("Fixed delay in seconds between retries."),
        ] = 1.0,
        env_load: Annotated[
            bool | None,
            Doc("Whether to read environment variables."),
        ] = None,
    ) -> Self:
        """Construct a retry policy with constant delay.

        Convenience factory for polling-style retries. Builds a
        [`ConstantBackoff`][grelmicro.resilience.ConstantBackoff]
        and forwards to the constructor.
        """
        backoff = ConstantBackoff(delay=delay)
        return cls(
            name,
            backoff,
            when=when,
            attempts=attempts,
            max_seconds=max_seconds,
            env_load=env_load,
        )

    def __aiter__(self) -> AsyncIterator[RetryAttempt]:
        """Yield successive attempts for async block-form usage."""
        state = self._state
        return _async_iter(state.config, state.matcher)

    def __iter__(self) -> Iterator[RetryAttempt]:
        """Yield successive attempts for sync block-form usage."""
        state = self._state
        return _sync_iter(state.config, state.matcher)

    @overload
    def __call__(
        self, fn: Callable[..., Awaitable[Any]], /
    ) -> Callable[..., Awaitable[Any]]: ...

    @overload
    def __call__(self, fn: Callable[..., Any], /) -> Callable[..., Any]: ...

    def __call__(self, fn: Callable[..., Any], /) -> Callable[..., Any]:
        """Decorate ``fn`` so each call runs through this retry policy."""
        if iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                state = self._state
                return await _run_async(
                    fn, args, kwargs, state.config, state.matcher, self._name
                )

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            state = self._state
            return _run_sync(
                fn, args, kwargs, state.config, state.matcher, self._name
            )

        return sync_wrapper

    async def _apply_reconfigure(self, new_config: RetryConfig) -> None:
        """Publish a fresh snapshot. In-flight loops keep their snapshot."""
        self._state = _State(config=new_config, matcher=new_config.when)


# --- Module-level decorator factory --------------------------------------


def _decorator(
    *,
    when: WhenInput,
    attempts: PositiveInt,
    backoff: RetryBackoffConfig | None,
) -> Callable[[F], F]:
    """Build a decorator from anonymous Retry kwargs."""
    config = RetryConfig(
        attempts=attempts,
        when=when,  # type: ignore[arg-type]
        backoff=backoff or ExponentialBackoff(),
    )
    matcher: Matcher = config.when

    def wrap(fn: F) -> F:
        if iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                return await _run_async(fn, args, kwargs, config, matcher)

            return async_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            return _run_sync(fn, args, kwargs, config, matcher)

        return sync_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

    return wrap


class _RetryFactory:
    """Decorator factory for the common case.

    Use ``@retry(when=..., attempts=...)`` for the default
    exponential backoff, or ``@retry.exponential(...)`` /
    ``@retry.constant(...)`` for the explicit forms.
    """

    def __call__(
        self,
        *,
        when: Annotated[
            WhenInput, Doc("Outcome filter that engages the retry.")
        ],
        attempts: Annotated[
            PositiveInt, Doc("Total calls including the first.")
        ] = 3,
        backoff: Annotated[
            RetryBackoffConfig | None,
            Doc("Backoff config. Default exponential + full jitter."),
        ] = None,
    ) -> Callable[[F], F]:
        """Build an anonymous retry decorator."""
        return _decorator(when=when, attempts=attempts, backoff=backoff)

    def exponential(
        self,
        *,
        when: WhenInput,
        attempts: PositiveInt = 3,
        base_delay: PositiveFloat = 0.1,
        max_delay: PositiveFloat = 30.0,
        jitter: Literal["none", "full", "decorrelated"] = "full",
    ) -> Callable[[F], F]:
        """Build a retry decorator with explicit exponential backoff."""
        return _decorator(
            when=when,
            attempts=attempts,
            backoff=ExponentialBackoff(
                base_delay=base_delay, max_delay=max_delay, jitter=jitter
            ),
        )

    def constant(
        self,
        *,
        when: WhenInput,
        attempts: PositiveInt = 3,
        delay: PositiveFloat = 1.0,
    ) -> Callable[[F], F]:
        """Build a retry decorator with constant backoff."""
        return _decorator(
            when=when,
            attempts=attempts,
            backoff=ConstantBackoff(delay=delay),
        )


retry = _RetryFactory()


# --- Module-level block-form factory -------------------------------------


class _RetryingFactory:
    """Async iterator factory for the block form.

    Use ``async for attempt in retrying(when=..., attempts=...):``
    for the default exponential backoff, or
    ``retrying.exponential(...)`` / ``retrying.constant(...)`` for
    the explicit forms.
    """

    def __call__(
        self,
        *,
        when: Annotated[
            WhenInput, Doc("Outcome filter that engages the retry.")
        ],
        attempts: Annotated[
            PositiveInt, Doc("Total calls including the first.")
        ] = 3,
        backoff: Annotated[
            RetryBackoffConfig | None,
            Doc("Backoff config. Default exponential + full jitter."),
        ] = None,
    ) -> AsyncIterator[RetryAttempt]:
        """Yield successive attempts for the block form."""
        config = RetryConfig(
            attempts=attempts,
            when=when,  # type: ignore[arg-type]
            backoff=backoff or ExponentialBackoff(),
        )
        return _async_iter(config, config.when)

    def exponential(
        self,
        *,
        when: WhenInput,
        attempts: PositiveInt = 3,
        base_delay: PositiveFloat = 0.1,
        max_delay: PositiveFloat = 30.0,
        jitter: Literal["none", "full", "decorrelated"] = "full",
    ) -> AsyncIterator[RetryAttempt]:
        """Yield attempts with explicit exponential backoff."""
        return self(
            when=when,
            attempts=attempts,
            backoff=ExponentialBackoff(
                base_delay=base_delay, max_delay=max_delay, jitter=jitter
            ),
        )

    def constant(
        self,
        *,
        when: WhenInput,
        attempts: PositiveInt = 3,
        delay: PositiveFloat = 1.0,
    ) -> AsyncIterator[RetryAttempt]:
        """Yield attempts with constant backoff."""
        return self(
            when=when,
            attempts=attempts,
            backoff=ConstantBackoff(delay=delay),
        )


retrying = _RetryingFactory()
