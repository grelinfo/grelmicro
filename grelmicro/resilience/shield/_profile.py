"""Shield profile configuration base class.

Defines the public fields shared by every profile config and exposes
the profile-specific algorithm parameters as class variables. The
algorithm parameters are frozen by profile choice and never appear
as Pydantic fields.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Annotated, Any, ClassVar, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ImportString,
    PositiveFloat,
    field_validator,
)
from pydantic_settings import NoDecode
from typing_extensions import Doc

from grelmicro._config import parse_csv_or_json
from grelmicro._guards import (
    is_class,
    is_instance,
    is_subclass,
    type_name,
)

__all__ = ["_BaseShieldConfig"]


def _normalize_entry(item: Any) -> type[BaseException]:  # noqa: ANN401
    """Return one entry as an exception class, refusing anything else.

    An entry that is neither a class nor a name is refused here rather
    than passed on, because the type check pydantic would run next reads
    `__class__`, which a lazy proxy raises from.
    """
    if is_instance(item, str):
        return _resolve_fqn(item)
    if not is_class(item):
        msg = (
            f"timeout_errors entry must be an exception class or a "
            f"fully-qualified name, got {type_name(item)}"
        )
        # `ValueError`, not `TypeError`: pydantic converts only `ValueError`
        # and `AssertionError`.
        raise ValueError(msg)
    return cast("type[BaseException]", item)


def _resolve_fqn(fqn: str) -> type[BaseException]:
    """Resolve a fully-qualified name to an exception class."""
    module_path, _, name = fqn.rpartition(".")
    if not module_path:
        msg = (
            "timeout_errors entry must be a fully-qualified name, "
            "such as 'httpx.HTTPError'"
        )
        raise ValueError(msg)
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        msg = "timeout_errors entry names a module that cannot be imported"
        raise ValueError(msg) from exc
    try:
        cls = getattr(module, name)
    except AttributeError as exc:
        msg = (
            "timeout_errors entry names an attribute its module does not define"
        )
        raise ValueError(msg) from exc
    if not (is_class(cls) and is_subclass(cls, Exception)):
        # `ValueError`, not `TypeError`: pydantic converts only `ValueError`
        # and `AssertionError` into a `ValidationError`, so a `TypeError` here
        # escaped `except SettingsValidationError` and `except ValueError` both.
        msg = "timeout_errors entry does not name an Exception subclass"
        raise ValueError(msg)
    return cls


class _BaseShieldConfig(
    BaseModel, frozen=True, extra="forbid", arbitrary_types_allowed=True
):
    """Base Shield configuration shared by every profile.

    Subclasses freeze the profile-specific algorithm parameters as
    `ClassVar` attributes and declare the `kind` literal for the
    discriminated union.
    """

    # --- Profile-frozen algorithm parameters (ClassVars) -----------------
    #
    # Subclasses set these. They are NOT Pydantic fields, so they never
    # appear in `model_dump()` and cannot be overridden per instance.

    max_consecutive_failures: ClassVar[int]
    initial_max_rate: ClassVar[float]
    adaptive_burst_capacity: ClassVar[float]
    min_rate_floor: ClassVar[float]
    initial_timeout: ClassVar[float]
    timeout_clamp_min: ClassVar[float]
    timeout_clamp_max: ClassVar[float]
    backoff_scale: ClassVar[float]
    backoff_cap: ClassVar[float]
    max_rate_cap_default: ClassVar[float | None] = None
    profile_name: ClassVar[str]

    # --- Public fields ---------------------------------------------------

    timeout_errors: Annotated[
        tuple[ImportString[type[BaseException]], ...],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc(
            "Exception classes treated as transient slow-down signals. "
            "Anything in this tuple (or its subclasses) is retried, "
            "shrinks the adaptive bucket, and consumes one retry-budget "
            "token. Anything else propagates unchanged. The effective "
            "tuple always includes `TimeoutError` regardless of the "
            "user value."
        ),
    ] = (TimeoutError,)

    max_rate: Annotated[
        PositiveFloat | None,
        Doc(
            "Optional hard ceiling on the adaptive bucket's rate in "
            "tokens per second, per worker process. Four workers each "
            "hold this ceiling, so the dependency sees four times it. "
            "`None` disables the cap."
        ),
    ] = None

    cache: Annotated[
        Any,
        Doc(
            "Optional cache instance used as a fallback on give-up. "
            "Must expose `async def get(key) -> value | None` and "
            "`async def set(key, value)`. Values returned by the "
            "wrapped function are written fire-and-forget on success."
        ),
    ] = None

    cache_key: Annotated[
        Callable[..., str] | None,
        Doc(
            "Optional callable that returns the cache key for a call. "
            "Receives the same `(*args, **kwargs)` as the wrapped "
            'function. Defaults to `f"{name}:{stable_hash(args, kwargs)}"`.'
        ),
    ] = None

    fallback: Annotated[
        Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None,
        Doc(
            "Optional callable invoked on give-up when the cache path "
            "does not return a value. Receives the underlying "
            "exception. May be sync or async."
        ),
    ] = None

    @field_validator("timeout_errors", mode="before")
    @classmethod
    def _normalize_timeout_errors(cls, value: Any) -> Any:  # noqa: ANN401
        """Accept a class, a tuple, or env CSV/JSON of FQNs.

        Every shape test goes through the total helpers the matcher uses.
        `isinstance` reads `__class__`, which a lazy proxy raises from,
        and a validator runs where an arbitrary error escapes the
        conversion pydantic performs for `ValueError` alone.
        """
        if value is None:
            return value
        if is_class(value):
            if not is_subclass(value, Exception):
                msg = (
                    f"timeout_errors entry {value.__name__} is not an "
                    f"Exception subclass. BaseException-only types are "
                    f"never retried."
                )
                # `ValueError`, not `TypeError`: pydantic converts only
                # `ValueError` and `AssertionError`.
                raise ValueError(msg)
            return (value,)
        if is_instance(value, str):
            parsed = parse_csv_or_json(value)
            if is_instance(parsed, list):
                return tuple(_normalize_entry(item) for item in parsed)
            return parsed  # pragma: no cover  # defensive: always a list here
        if is_instance(value, list | tuple):
            return tuple(_normalize_entry(item) for item in value)
        msg = (
            f"timeout_errors must be an exception class, a tuple of them, or "
            f"a fully-qualified name, got {type_name(value)}"
        )
        # `ValueError`, not `TypeError`: pydantic converts only `ValueError`
        # and `AssertionError`. Refused here rather than passed on, because
        # the type check pydantic would run next reads `__class__` too.
        raise ValueError(msg)

    def effective_timeout_errors(self) -> tuple[type[BaseException], ...]:
        """Return the `timeout_errors` tuple with `TimeoutError` appended.

        `TimeoutError` is always retryable because Shield's own
        per-attempt timeout surfaces as a `TimeoutError`.
        """
        if any(
            is_class(exc) and is_subclass(TimeoutError, exc)
            for exc in self.timeout_errors
        ):
            return tuple(self.timeout_errors)
        return (*self.timeout_errors, TimeoutError)
