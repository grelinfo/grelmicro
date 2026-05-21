"""Fallback."""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from importlib import import_module
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, field_validator, model_validator
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    _build_settings_cls,
    env_load_default,
    env_segment,
    parse_csv_or_json,
)
from grelmicro._json import json_loads
from grelmicro.resilience._match import Match, Matcher
from grelmicro.resilience._outcome import Outcome

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience.retry import WhenInput

__all__ = [
    "Fallback",
    "FallbackConfig",
    "FallbackResult",
    "fallback",
    "falling_back",
]


# Sentinel used to distinguish "default not provided" from "default is None".
# ``None`` is a valid fallback value, so we cannot use it as the unset marker.
_UNSET: Any = object()


def _coerce_to_match(value: Any) -> Match:  # noqa: ANN401
    """Coerce a non-Match shorthand into a ``Match`` instance."""
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


class FallbackConfig(BaseModel, frozen=True, extra="forbid"):
    """Fallback policy configuration.

    Holds the exception filter plus exactly one of ``default`` or
    ``factory``. Frozen Pydantic data class. Three-paths configuration:
    kwargs, instance, or env vars.

    Read more in the [Fallback](../resilience/fallback.md) docs.
    """

    when: Annotated[
        Match,
        Doc(
            "Exception filter that engages the fallback. Pass a "
            "[`Match`][grelmicro.resilience.Match] or a shorthand: "
            "an exception class, a tuple of classes, or a predicate "
            "on the exception. ``BaseException`` subclasses outside "
            "``Exception`` are never caught."
        ),
    ]

    default: Annotated[
        Any,
        Doc(
            "Static value returned when ``when`` matches. Mutually "
            "exclusive with ``factory``. Exactly one must be set."
        ),
    ] = _UNSET

    factory: Annotated[
        Callable[[BaseException], Any] | None,
        Doc(
            "Callable that produces the fallback value from the "
            "exception. Mutually exclusive with ``default``."
        ),
    ] = None

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("when", mode="before")
    @classmethod
    def _coerce_when(cls, value: Any) -> Any:  # noqa: ANN401
        """Coerce shorthand shapes (and the env string) to a ``Match``."""
        if isinstance(value, Match):
            return value
        if isinstance(value, str):
            value = parse_csv_or_json(value)
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

    @model_validator(mode="after")
    def _check_mutual_exclusion(self) -> Self:
        """Enforce that exactly one of ``default`` / ``factory`` is set."""
        has_default = self.default is not _UNSET
        has_factory = self.factory is not None
        if has_default and has_factory:
            msg = "FallbackConfig: pass either default= or factory=, not both"
            raise ValueError(msg)
        if not has_default and not has_factory:
            msg = (
                "FallbackConfig: exactly one of default= or factory= "
                "must be set"
            )
            raise ValueError(msg)
        return self


def _resolve_value(config: FallbackConfig, exc: BaseException) -> Any:  # noqa: ANN401
    """Compute the fallback value for a matched exception."""
    if config.factory is not None:
        return config.factory(exc)
    return config.default


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its compiled matcher."""

    config: FallbackConfig
    matcher: Matcher


def _resolve_config(
    name: str,
    *,
    when: WhenInput | None,
    default: Any,  # noqa: ANN401
    factory: Callable[[BaseException], Any] | None,
    config: FallbackConfig | None,
    env_load: bool | None,
) -> FallbackConfig:
    """Build a `FallbackConfig` from kwargs, an explicit config, or env.

    Mirrors :func:`grelmicro._config.resolve_config` but treats
    ``default`` with the ``_UNSET`` sentinel so that ``default=None``
    is preserved as a valid value.
    """
    explicit_kwargs = (
        when is not None or factory is not None or default is not _UNSET
    )
    if config is not None:
        if explicit_kwargs:
            msg = "pass a pre-built config OR individual kwargs, not both"
            raise TypeError(msg)
        return config

    kwargs: dict[str, Any] = {}
    if when is not None:
        kwargs["when"] = when
    if factory is not None:
        kwargs["factory"] = factory
    if default is not _UNSET:
        kwargs["default"] = default

    if env_load is None:
        env_load = env_load_default()
    if not env_load:
        return FallbackConfig.model_validate(kwargs)

    env_prefix = f"GREL_FALLBACK_{env_segment(name)}_"
    _load_default_from_env(kwargs, env_prefix)
    settings_cls = _build_settings_cls(FallbackConfig, env_prefix)
    return settings_cls(**kwargs)  # type: ignore[return-value]  # ty: ignore[invalid-return-type]


def _load_default_from_env(kwargs: dict[str, Any], env_prefix: str) -> None:
    """Pre-parse the ``DEFAULT`` env var as JSON before BaseSettings reads it.

    `default` accepts any value, so pydantic-settings would pass an env
    string straight through as a string. Pre-parsing here (only on the
    env path) turns ``GREL_FALLBACK_X_DEFAULT=[]`` into a list and
    ``=null`` into ``None``. Strings that fail to parse are kept
    verbatim. Direct ``default=...`` kwargs are never reinterpreted.
    """
    if "default" in kwargs:
        return
    raw = os.environ.get(f"{env_prefix}DEFAULT")
    if raw is None:
        return
    try:
        kwargs["default"] = json_loads(raw)
    except Exception:  # noqa: BLE001
        kwargs["default"] = raw


class FallbackResult[T]:
    """Holder for the value produced inside a `falling_back` block.

    Call [`set(value)`][grelmicro.resilience.FallbackResult.set]
    on success. When the block raises an exception matching ``when=``,
    the exception is suppressed and the holder is filled with the
    configured default (or the factory output). Access the resulting
    value with the
    [`value`][grelmicro.resilience.FallbackResult.value] property
    after the block exits.
    """

    __slots__ = ("_value", "_was_set")

    def __init__(self) -> None:
        """Initialize an empty result holder."""
        self._value: Any = None
        self._was_set = False

    def set(self, value: T) -> None:
        """Record the success value for the block."""
        self._value = value
        self._was_set = True

    @property
    def value(self) -> T:
        """Return the recorded value (success or fallback).

        Raises:
            RuntimeError: When accessed before any value was set.
        """
        if not self._was_set:
            msg = (
                "FallbackResult.value accessed before any value was set. "
                "Call result.set(...) inside the block, or rely on the "
                "fallback path to fill it when an exception is matched."
            )
            raise RuntimeError(msg)
        return self._value  # type: ignore[no-any-return]


class _FallbackBlock[T]:
    """Context manager for the `falling_back` block form."""

    __slots__ = ("_config", "_matcher", "_result")

    def __init__(self, config: FallbackConfig, matcher: Matcher) -> None:
        self._config = config
        self._matcher = matcher
        self._result: FallbackResult[T] = FallbackResult()

    def __enter__(self) -> FallbackResult[T]:
        """Enter the block (sync)."""
        return self._result

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Suppress matched exceptions and fill the result."""
        return self._handle_exit(exc)

    async def __aenter__(self) -> FallbackResult[T]:
        """Enter the block (async)."""
        return self._result

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Suppress matched exceptions and fill the result."""
        return self._handle_exit(exc)

    def _handle_exit(self, exc: BaseException | None) -> bool:
        if exc is None:
            return False
        # Never catch CancelledError, KeyboardInterrupt, SystemExit.
        if not isinstance(exc, Exception):
            return False
        if not self._matcher(Outcome.from_exception(exc)):
            return False
        self._result.set(_resolve_value(self._config, exc))
        return True


class Fallback(Reconfigurable[FallbackConfig]):
    """Fallback policy.

    A named, reusable fallback policy with three-paths configuration
    and live reconfiguration. Use the constructor for the common
    case and `Fallback.from_config` when configuration is assembled
    elsewhere.

    Read more in the [Fallback](../resilience/fallback.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The name of the fallback policy. Used as the env "
                "namespace and exposed via the ``name`` property."
            ),
        ],
        *,
        when: Annotated[
            WhenInput | None,
            Doc(
                "Exception filter that engages the fallback. Pass a "
                "[`Match`][grelmicro.resilience.Match] or a shorthand "
                "(class, tuple of classes, callable). Required unless "
                "``config=`` is given or the value comes from env."
            ),
        ] = None,
        default: Annotated[  # noqa: ANN401
            Any,
            Doc(
                "Static fallback value. Mutually exclusive with "
                "``factory``. ``None`` is a valid value."
            ),
        ] = _UNSET,
        factory: Annotated[
            Callable[[BaseException], Any] | None,
            Doc(
                "Callable that produces the fallback value from the "
                "matched exception. Mutually exclusive with ``default``."
            ),
        ] = None,
        config: Annotated[
            FallbackConfig | None,
            Doc(
                "A pre-built [`FallbackConfig`][grelmicro.resilience.FallbackConfig]. "
                "Mutually exclusive with the per-field kwargs."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults to "
                "the process-wide ``GREL_ENV_LOAD`` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the fallback policy."""
        self._name = name
        resolved = _resolve_config(
            name,
            when=when,
            default=default,
            factory=factory,
            config=config,
            env_load=env_load,
        )
        self._config = resolved
        self._state = _State(config=resolved, matcher=resolved.when)
        self._reconfigure_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Return the fallback policy identity."""
        return self._name

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("The name of the fallback policy.")],
        config: Annotated[
            FallbackConfig,
            Doc("The pre-built fallback configuration."),
        ],
    ) -> Self:
        """Construct a `Fallback` from a name and a pre-built `FallbackConfig`."""
        return cls(name, config=config)

    def __call__(self, fn: Callable[..., Any], /) -> Callable[..., Any]:
        """Decorate ``fn`` so each call runs through this fallback policy."""
        if iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                state = self._state
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if not state.matcher(Outcome.from_exception(exc)):
                        raise
                    return _resolve_value(state.config, exc)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            state = self._state
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if not state.matcher(Outcome.from_exception(exc)):
                    raise
                return _resolve_value(state.config, exc)

        return sync_wrapper

    async def _apply_reconfigure(self, new_config: FallbackConfig) -> None:
        """Publish a fresh snapshot."""
        self._state = _State(config=new_config, matcher=new_config.when)


def fallback(
    *,
    when: Annotated[
        WhenInput, Doc("Exception filter that engages the fallback.")
    ],
    default: Annotated[  # noqa: ANN401
        Any, Doc("Static fallback value. Mutually exclusive with factory.")
    ] = _UNSET,
    factory: Annotated[
        Callable[[BaseException], Any] | None,
        Doc("Callable producing the fallback value from the exception."),
    ] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build an anonymous fallback decorator.

    Exactly one of ``default=`` or ``factory=`` must be set.
    """
    kwargs: dict[str, Any] = {"when": when, "factory": factory}
    if default is not _UNSET:
        kwargs["default"] = default
    config = FallbackConfig.model_validate(kwargs)
    matcher: Matcher = config.when

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if not matcher(Outcome.from_exception(exc)):
                        raise
                    return _resolve_value(config, exc)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if not matcher(Outcome.from_exception(exc)):
                    raise
                return _resolve_value(config, exc)

        return sync_wrapper

    return wrap


def falling_back(
    *,
    when: Annotated[
        WhenInput, Doc("Exception filter that engages the fallback.")
    ],
    default: Annotated[  # noqa: ANN401
        Any, Doc("Static fallback value. Mutually exclusive with factory.")
    ] = _UNSET,
    factory: Annotated[
        Callable[[BaseException], Any] | None,
        Doc("Callable producing the fallback value from the exception."),
    ] = None,
) -> _FallbackBlock[Any]:
    """Build a fallback context manager for the block form.

    Use ``async with falling_back(...) as result:`` (or the sync
    ``with`` form) to wrap a block of statements. Call
    ``result.set(value)`` on the success path. On a matched
    exception, the exception is suppressed and ``result.value``
    holds the configured default or factory output.

    Exactly one of ``default=`` or ``factory=`` must be set.
    """
    kwargs: dict[str, Any] = {"when": when, "factory": factory}
    if default is not _UNSET:
        kwargs["default"] = default
    config = FallbackConfig.model_validate(kwargs)
    return _FallbackBlock(config, config.when)
