"""Shield profile configuration base class.

Defines the public fields shared by every profile config and exposes
the profile-specific algorithm parameters as class variables. The
algorithm parameters are frozen by profile choice and never appear
as Pydantic fields.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable  # noqa: TC003
from importlib import import_module
from typing import Annotated, Any, ClassVar

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

__all__ = ["_BaseShieldConfig"]


def _resolve_fqn(fqn: str) -> type[BaseException]:
    """Resolve a fully-qualified name to an exception class."""
    module_path, _, name = fqn.rpartition(".")
    if not module_path:
        msg = (
            f"timeout_errors entry must be a fully-qualified name, got {fqn!r}"
        )
        raise ValueError(msg)
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        msg = (
            f"timeout_errors entry {fqn!r}: cannot import module "
            f"{module_path!r} ({exc})"
        )
        raise ValueError(msg) from exc
    try:
        cls = getattr(module, name)
    except AttributeError as exc:
        msg = (
            f"timeout_errors entry {fqn!r}: module {module_path!r} has "
            f"no attribute {name!r}"
        )
        raise ValueError(msg) from exc
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        msg = f"timeout_errors entry {fqn!r} is not an Exception subclass"
        raise TypeError(msg)
    return cls


class _BaseShieldConfig(BaseModel, frozen=True, extra="forbid"):
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
            "tokens per second. `None` disables the cap."
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

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("timeout_errors", mode="before")
    @classmethod
    def _normalize_timeout_errors(cls, value: Any) -> Any:  # noqa: ANN401
        """Accept a class, a tuple, or env CSV/JSON of FQNs."""
        if value is None:
            return value
        if isinstance(value, type):
            if not issubclass(value, Exception):
                msg = (
                    f"timeout_errors entry {value!r} is not an Exception "
                    f"subclass; BaseException-only types are never retried."
                )
                raise TypeError(msg)
            return (value,)
        if isinstance(value, str):
            parsed = parse_csv_or_json(value)
            if isinstance(parsed, list):
                return tuple(
                    _resolve_fqn(item) if isinstance(item, str) else item
                    for item in parsed
                )
            return parsed  # pragma: no cover  # defensive: always a list here
        if isinstance(value, list | tuple):
            return tuple(
                _resolve_fqn(item) if isinstance(item, str) else item
                for item in value
            )
        return value

    def effective_timeout_errors(self) -> tuple[type[BaseException], ...]:
        """Return the `timeout_errors` tuple with `TimeoutError` appended.

        `TimeoutError` is always retryable because Shield's own
        per-attempt timeout surfaces as a `TimeoutError`.
        """
        if any(
            isinstance(exc, type) and issubclass(TimeoutError, exc)
            for exc in self.timeout_errors
        ):
            return tuple(self.timeout_errors)
        return (*self.timeout_errors, TimeoutError)
