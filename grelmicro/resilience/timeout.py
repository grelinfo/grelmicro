"""Timeout."""

from __future__ import annotations

import asyncio
import functools
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    env_segment,
    resolve_config,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

__all__ = [
    "Timeout",
    "TimeoutConfig",
]


class TimeoutConfig(BaseModel, frozen=True, extra="forbid"):
    """Timeout policy configuration.

    Frozen Pydantic data class. Three-paths configuration: kwargs,
    instance, or env vars.

    Read more in the [Timeout](../resilience/timeout.md) docs.
    """

    seconds: Annotated[
        PositiveFloat,
        Doc(
            "Deadline in seconds. The inner block is cancelled and "
            "``TimeoutError`` is raised when the deadline elapses."
        ),
    ]


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot of the timeout config."""

    config: TimeoutConfig


class Timeout(Reconfigurable[TimeoutConfig]):
    """Timeout policy.

    A named, reusable async deadline with three-paths configuration
    and live reconfiguration. Use as an async context manager or as
    a decorator on async functions.

    Read more in the [Timeout](../resilience/timeout.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The name of the timeout policy. Used as the env "
                "namespace and exposed via the ``name`` property."
            ),
        ],
        *,
        seconds: Annotated[
            PositiveFloat | None,
            Doc(
                "Deadline in seconds. Required unless ``config=`` is "
                "given or the value comes from env."
            ),
        ] = None,
        config: Annotated[
            TimeoutConfig | None,
            Doc(
                "A pre-built [`TimeoutConfig`][grelmicro.resilience.TimeoutConfig]. "
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
        """Initialize the timeout policy."""
        self._name = name
        resolved = resolve_config(
            TimeoutConfig,
            explicit=config,
            kwargs={"seconds": seconds},
            env_prefix=f"GREL_TIMEOUT_{env_segment(name)}_",
            env_load=env_load,
        )
        self._config = resolved
        self._state = _State(config=resolved)
        self._reconfigure_lock = asyncio.Lock()
        self._scopes: ContextVar[tuple[asyncio.Timeout, ...]] = ContextVar(
            f"grelmicro.resilience.timeout.{name}", default=()
        )

    @property
    def name(self) -> str:
        """Return the timeout policy identity."""
        return self._name

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("The name of the timeout policy.")],
        config: Annotated[
            TimeoutConfig,
            Doc("The pre-built timeout configuration."),
        ],
    ) -> Self:
        """Construct a `Timeout` from a name and a pre-built `TimeoutConfig`."""
        return cls(name, config=config)

    async def __aenter__(self) -> Self:
        """Open a fresh deadline scope for the current task."""
        scope = asyncio.timeout(self._state.config.seconds)
        await scope.__aenter__()
        self._scopes.set((*self._scopes.get(), scope))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the most recently opened scope for the current task."""
        stack = self._scopes.get()
        scope = stack[-1]
        self._scopes.set(stack[:-1])
        return await scope.__aexit__(exc_type, exc, tb)

    def __call__(
        self, fn: Callable[..., Awaitable[Any]], /
    ) -> Callable[..., Awaitable[Any]]:
        """Decorate ``fn`` so each call runs under this timeout."""
        if not iscoroutinefunction(fn):
            msg = (
                "Timeout only decorates async functions. asyncio cannot "
                f"cancel sync code, got {fn!r}."
            )
            raise TypeError(msg)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            async with self:
                return await fn(*args, **kwargs)

        return async_wrapper

    async def _apply_reconfigure(self, new_config: TimeoutConfig) -> None:
        """Publish a fresh snapshot. In-flight scopes keep their deadline."""
        self._state = _State(config=new_config)
