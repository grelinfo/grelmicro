"""Retry."""

import asyncio
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from inspect import iscoroutinefunction
from logging import getLogger
from types import TracebackType
from typing import (
    Annotated,
    Any,
    Literal,
    Self,
    TypeVar,
    overload,
)

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    ImportString,
    PositiveFloat,
    PositiveInt,
)
from pydantic_settings import NoDecode
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    env_segment,
    parse_csv_or_json,
    resolve_config,
)
from grelmicro.resilience._retry_strategy import build_retry_strategy
from grelmicro.resilience.backoffs import (
    ConstantBackoffConfig,
    ExponentialBackoffConfig,
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

ExceptionFilter = (
    type[BaseException]
    | tuple[type[BaseException], ...]
    | Callable[[BaseException], bool]
)
"""User-facing filter accepted by ``on=``.

A class, a tuple of classes, or a predicate callable.
"""


def _normalize_filter(
    on: ExceptionFilter,
) -> tuple[type[BaseException], ...] | Callable[[BaseException], bool]:
    """Normalize ``on`` to a tuple of classes or a predicate."""
    if isinstance(on, type):
        return (on,)  # type: ignore[return-value]  # ty: ignore[invalid-return-type]
    return on


def _matches(
    exc: BaseException,
    matcher: tuple[type[BaseException], ...] | Callable[[BaseException], bool],
) -> bool:
    """Return True if ``exc`` matches ``matcher``."""
    if not isinstance(matcher, tuple):
        return matcher(exc)
    return isinstance(exc, matcher)


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

    on: Annotated[
        tuple[ImportString[type[BaseException]], ...]
        | Callable[[BaseException], bool],
        BeforeValidator(parse_csv_or_json),
        NoDecode,
        Doc(
            "Filter for exceptions that trigger a retry. Pass a "
            "class, a tuple of classes, or a predicate callable. "
            "Required: there is no default."
        ),
    ]

    backoff: Annotated[
        RetryBackoffConfig,
        Field(default_factory=ExponentialBackoffConfig),
        Doc(
            "Backoff algorithm config. Discriminated union of "
            "[`ExponentialBackoffConfig`][grelmicro.resilience.ExponentialBackoffConfig] "
            "and [`ConstantBackoffConfig`][grelmicro.resilience.ConstantBackoffConfig]. "
            "Default: exponential with full jitter."
        ),
    ]


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with no live strategy.

    A fresh ``RetryStrategy`` is built per loop from
    ``state.config.backoff``, so the snapshot only carries the
    immutable config. Reconfigure swaps the snapshot atomically.
    """

    config: RetryConfig


class RetryAttempt:
    """One iteration of a retry loop.

    Yielded by [`Retry`][grelmicro.resilience.Retry] iteration and
    by [`retrying`][grelmicro.resilience.retrying]. Used as an
    async (or sync) context manager. Suppresses retryable
    exceptions until attempts are exhausted, then re-raises the
    underlying error with a PEP 678 note.
    """

    __slots__ = (
        "_attempts",
        "_loop",
        "_matcher",
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
        matcher: tuple[type[BaseException], ...]
        | Callable[[BaseException], bool],
        loop: "_AttemptLoop",
        started_at: float,
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
        if not _matches(exc, self._matcher):
            return False
        if self.number >= self._attempts:
            elapsed = time.monotonic() - self._started_at
            backoff_name = self._loop.backoff_name
            exc.add_note(
                f"retry: {self._attempts}/{self._attempts} attempts "
                f"exhausted in {elapsed:.2f}s ({backoff_name} backoff)"
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
    return config.type


async def _async_iter(
    config: RetryConfig,
    matcher: tuple[type[BaseException], ...] | Callable[[BaseException], bool],
) -> AsyncIterator[RetryAttempt]:
    """Yield successive ``RetryAttempt`` objects, sleeping between attempts."""
    strategy = build_retry_strategy(config.backoff)
    loop = _AttemptLoop(_backoff_name(config.backoff))
    started_at = time.monotonic()
    delay_before = 0.0
    for number in range(1, config.attempts + 1):
        if delay_before > 0:
            await asyncio.sleep(delay_before)
        yield RetryAttempt(
            number=number,
            delay_before=delay_before,
            attempts=config.attempts,
            matcher=matcher,
            loop=loop,
            started_at=started_at,
        )
        if loop.done:
            return
        delay_before = strategy.delay(number)


def _sync_iter(
    config: RetryConfig,
    matcher: tuple[type[BaseException], ...] | Callable[[BaseException], bool],
) -> Iterator[RetryAttempt]:
    """Yield successive ``RetryAttempt`` objects, sleeping between attempts."""
    strategy = build_retry_strategy(config.backoff)
    loop = _AttemptLoop(_backoff_name(config.backoff))
    started_at = time.monotonic()
    delay_before = 0.0
    for number in range(1, config.attempts + 1):
        if delay_before > 0:
            time.sleep(delay_before)
        yield RetryAttempt(
            number=number,
            delay_before=delay_before,
            attempts=config.attempts,
            matcher=matcher,
            loop=loop,
            started_at=started_at,
        )
        if loop.done:
            return
        delay_before = strategy.delay(number)


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
                "The backoff algorithm config. Pass a "
                "[`ExponentialBackoffConfig`][grelmicro.resilience.ExponentialBackoffConfig] "
                "or [`ConstantBackoffConfig`][grelmicro.resilience.ConstantBackoffConfig], "
                "or omit for the default exponential + full jitter."
            ),
        ] = None,
        *,
        on: Annotated[
            ExceptionFilter | None,
            Doc(
                "Filter for exceptions that trigger a retry. Pass "
                "a class, a tuple of classes, or a predicate "
                "callable. Required unless ``config=`` is given."
            ),
        ] = None,
        attempts: Annotated[
            PositiveInt | None,
            Doc("Total calls including the first. Default ``3``."),
        ] = None,
        config: Annotated[
            RetryConfig | None,
            Doc(
                "A pre-built [`RetryConfig`][grelmicro.resilience.RetryConfig]. "
                "Mutually exclusive with the per-field kwargs."
            ),
        ] = None,
        read_env: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults "
                "to the process-wide ``GREL_CONFIG_FROM_ENV`` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the retry policy."""
        self._name = name
        kwargs: dict[str, object | None] = {
            "attempts": attempts,
            "on": _normalize_filter(on) if on is not None else None,
            "backoff": backoff,
        }
        resolved = resolve_config(
            RetryConfig,
            explicit=config,
            kwargs=kwargs,
            env_prefix=f"GREL_RETRY_{env_segment(name)}_",
            read_env=read_env,
        )
        self._config = resolved
        self._state = _State(config=resolved)
        self._reconfigure_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Return the retry policy identity."""
        return self._name

    @classmethod
    def exponential(
        cls,
        name: Annotated[str, Doc("The name of the retry policy.")],
        *,
        on: Annotated[
            ExceptionFilter,
            Doc("Filter for exceptions that trigger a retry."),
        ],
        attempts: Annotated[
            PositiveInt,
            Doc("Total calls including the first."),
        ] = 3,
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
        read_env: Annotated[
            bool | None,
            Doc("Whether to read environment variables."),
        ] = None,
    ) -> Self:
        """Construct a retry policy with exponential backoff.

        Convenience factory for the common case. Builds an
        [`ExponentialBackoffConfig`][grelmicro.resilience.ExponentialBackoffConfig]
        and forwards to the constructor.
        """
        backoff = ExponentialBackoffConfig(
            base_delay=base_delay, max_delay=max_delay, jitter=jitter
        )
        return cls(
            name,
            backoff,
            on=on,
            attempts=attempts,
            read_env=read_env,
        )

    @classmethod
    def constant(
        cls,
        name: Annotated[str, Doc("The name of the retry policy.")],
        *,
        on: Annotated[
            ExceptionFilter,
            Doc("Filter for exceptions that trigger a retry."),
        ],
        attempts: Annotated[
            PositiveInt,
            Doc("Total calls including the first."),
        ] = 3,
        delay: Annotated[
            PositiveFloat,
            Doc("Fixed delay in seconds between retries."),
        ] = 1.0,
        read_env: Annotated[
            bool | None,
            Doc("Whether to read environment variables."),
        ] = None,
    ) -> Self:
        """Construct a retry policy with constant delay.

        Convenience factory for polling-style retries. Builds a
        [`ConstantBackoffConfig`][grelmicro.resilience.ConstantBackoffConfig]
        and forwards to the constructor.
        """
        backoff = ConstantBackoffConfig(delay=delay)
        return cls(
            name,
            backoff,
            on=on,
            attempts=attempts,
            read_env=read_env,
        )

    def __aiter__(self) -> AsyncIterator[RetryAttempt]:
        """Yield successive attempts for async block-form usage."""
        config = self._state.config
        matcher = _normalize_filter(config.on)  # type: ignore[arg-type]
        return _async_iter(config, matcher)

    def __iter__(self) -> Iterator[RetryAttempt]:
        """Yield successive attempts for sync block-form usage."""
        config = self._state.config
        matcher = _normalize_filter(config.on)  # type: ignore[arg-type]
        return _sync_iter(config, matcher)

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
                async for attempt in self:
                    async with attempt:
                        return await fn(*args, **kwargs)
                msg = "retry: unreachable"  # pragma: no cover
                raise RuntimeError(msg)  # pragma: no cover

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            for attempt in self:
                with attempt:
                    return fn(*args, **kwargs)
            msg = "retry: unreachable"  # pragma: no cover
            raise RuntimeError(msg)  # pragma: no cover

        return sync_wrapper

    async def _apply_reconfigure(self, new_config: RetryConfig) -> None:
        """Publish a fresh snapshot. In-flight loops keep their snapshot."""
        self._state = _State(config=new_config)


# --- Module-level decorator factory --------------------------------------


def _decorator(
    *,
    on: ExceptionFilter,
    attempts: PositiveInt,
    backoff: RetryBackoffConfig | None,
) -> Callable[[F], F]:
    """Build a decorator from anonymous Retry kwargs."""
    matcher = _normalize_filter(on)
    config = RetryConfig.model_construct(
        attempts=attempts,
        on=matcher,
        backoff=backoff or ExponentialBackoffConfig(),
    )

    def wrap(fn: F) -> F:
        if iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                async for attempt in _async_iter(config, matcher):
                    async with attempt:
                        return await fn(*args, **kwargs)
                msg = "retry: unreachable"  # pragma: no cover
                raise RuntimeError(msg)  # pragma: no cover

            return async_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            for attempt in _sync_iter(config, matcher):
                with attempt:
                    return fn(*args, **kwargs)
            msg = "retry: unreachable"  # pragma: no cover
            raise RuntimeError(msg)  # pragma: no cover

        return sync_wrapper  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

    return wrap


class _RetryFactory:
    """Decorator factory for the common case.

    Use ``@retry(on=..., attempts=...)`` for the default
    exponential backoff, or ``@retry.exponential(...)`` /
    ``@retry.constant(...)`` for the explicit forms.
    """

    def __call__(
        self,
        *,
        on: Annotated[
            ExceptionFilter, Doc("Filter for exceptions that trigger a retry.")
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
        return _decorator(on=on, attempts=attempts, backoff=backoff)

    def exponential(
        self,
        *,
        on: ExceptionFilter,
        attempts: PositiveInt = 3,
        base_delay: PositiveFloat = 0.1,
        max_delay: PositiveFloat = 30.0,
        jitter: Literal["none", "full", "decorrelated"] = "full",
    ) -> Callable[[F], F]:
        """Build a retry decorator with explicit exponential backoff."""
        return _decorator(
            on=on,
            attempts=attempts,
            backoff=ExponentialBackoffConfig(
                base_delay=base_delay, max_delay=max_delay, jitter=jitter
            ),
        )

    def constant(
        self,
        *,
        on: ExceptionFilter,
        attempts: PositiveInt = 3,
        delay: PositiveFloat = 1.0,
    ) -> Callable[[F], F]:
        """Build a retry decorator with constant backoff."""
        return _decorator(
            on=on,
            attempts=attempts,
            backoff=ConstantBackoffConfig(delay=delay),
        )


retry = _RetryFactory()


# --- Module-level block-form factory -------------------------------------


class _RetryingFactory:
    """Async iterator factory for the block form.

    Use ``async for attempt in retrying(on=..., attempts=...):`` for
    the default exponential backoff, or
    ``retrying.exponential(...)`` / ``retrying.constant(...)`` for
    the explicit forms.
    """

    def __call__(
        self,
        *,
        on: Annotated[
            ExceptionFilter, Doc("Filter for exceptions that trigger a retry.")
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
        matcher = _normalize_filter(on)
        config = RetryConfig.model_construct(
            attempts=attempts,
            on=matcher,
            backoff=backoff or ExponentialBackoffConfig(),
        )
        return _async_iter(config, matcher)

    def exponential(
        self,
        *,
        on: ExceptionFilter,
        attempts: PositiveInt = 3,
        base_delay: PositiveFloat = 0.1,
        max_delay: PositiveFloat = 30.0,
        jitter: Literal["none", "full", "decorrelated"] = "full",
    ) -> AsyncIterator[RetryAttempt]:
        """Yield attempts with explicit exponential backoff."""
        return self(
            on=on,
            attempts=attempts,
            backoff=ExponentialBackoffConfig(
                base_delay=base_delay, max_delay=max_delay, jitter=jitter
            ),
        )

    def constant(
        self,
        *,
        on: ExceptionFilter,
        attempts: PositiveInt = 3,
        delay: PositiveFloat = 1.0,
    ) -> AsyncIterator[RetryAttempt]:
        """Yield attempts with constant backoff."""
        return self(
            on=on,
            attempts=attempts,
            backoff=ConstantBackoffConfig(delay=delay),
        )


retrying = _RetryingFactory()
