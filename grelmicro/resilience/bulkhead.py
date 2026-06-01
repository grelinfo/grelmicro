"""Bulkhead."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, NonNegativeFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._config import Reconfigurable, env_segment, resolve_config
from grelmicro.resilience.errors import BulkheadFullError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

__all__ = [
    "Bulkhead",
    "BulkheadConfig",
]


def _current_task() -> asyncio.Task[Any]:
    """Return the current asyncio task or raise if none is running."""
    task = asyncio.current_task()
    if task is None:  # pragma: no cover
        msg = "Bulkhead requires a running asyncio task"
        raise RuntimeError(msg)
    return task


class BulkheadConfig(BaseModel, frozen=True, extra="forbid"):
    """Bulkhead policy configuration.

    Frozen Pydantic data class. Three-paths configuration: kwargs,
    instance, or env vars.

    Read more in the [Bulkhead](../resilience/bulkhead.md) docs.
    """

    max_concurrent: Annotated[
        PositiveInt | None,
        Doc(
            "Maximum concurrent calls admitted to the bulkhead. `None` "
            "(the default) leaves concurrency unbounded."
        ),
    ] = None

    max_wait: Annotated[
        NonNegativeFloat | None,
        Doc(
            "Seconds a caller waits for a free permit before the "
            "bulkhead rejects it with `BulkheadFullError`. `None` (the "
            "default) and `0` reject immediately (fail fast). Ignored "
            "when `max_concurrent` is `None`."
        ),
    ] = None

    max_workers: Annotated[
        PositiveInt | None,
        Doc(
            "Size of the private thread pool backing `to_thread`. `None` "
            "(the default) uses the event loop's shared executor."
        ),
    ] = None


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the config with its bound semaphore."""

    config: BulkheadConfig
    semaphore: asyncio.Semaphore | None


class Bulkhead(Reconfigurable[BulkheadConfig]):
    """Bulkhead policy.

    A named, reusable concurrency limiter with three-paths
    configuration and live reconfiguration. Use it as an async context
    manager or as a decorator on async functions to bound the number of
    in-flight calls, and `to_thread` to run blocking work on a bounded
    private thread pool.

    When the bulkhead is full, a caller waits up to `max_wait` seconds
    for a permit, then is rejected with
    [`BulkheadFullError`][grelmicro.resilience.BulkheadFullError]. The
    default fails fast (no wait).

    Read more in the [Bulkhead](../resilience/bulkhead.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The name of the bulkhead. Used as the env namespace, "
                "the rejection error label, and the thread-name prefix."
            ),
        ],
        *,
        max_concurrent: Annotated[
            PositiveInt | None,
            Doc("Maximum concurrent calls. `None` leaves it unbounded."),
        ] = None,
        max_wait: Annotated[
            NonNegativeFloat | None,
            Doc(
                "Seconds to wait for a permit before rejecting. `None` "
                "or `0` fails fast."
            ),
        ] = None,
        max_workers: Annotated[
            PositiveInt | None,
            Doc("Private thread-pool size for `to_thread`."),
        ] = None,
        config: Annotated[
            BulkheadConfig | None,
            Doc(
                "A pre-built [`BulkheadConfig`][grelmicro.resilience.BulkheadConfig]. "
                "Mutually exclusive with the per-field kwargs."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults to the "
                "process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the bulkhead."""
        self._name = name
        resolved = resolve_config(
            BulkheadConfig,
            explicit=config,
            kwargs={
                "max_concurrent": max_concurrent,
                "max_wait": max_wait,
                "max_workers": max_workers,
            },
            env_prefix=f"GREL_BULKHEAD_{env_segment(name)}_",
            env_load=env_load,
        )
        self._config = resolved
        self._state = _State(
            config=resolved, semaphore=_build_semaphore(resolved)
        )
        self._reconfigure_lock = asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._permits: dict[
            asyncio.Task[Any], list[asyncio.Semaphore | None]
        ] = {}

    @property
    def name(self) -> str:
        """Return the bulkhead identity."""
        return self._name

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("The name of the bulkhead.")],
        config: Annotated[
            BulkheadConfig,
            Doc("The pre-built bulkhead configuration."),
        ],
    ) -> Self:
        """Construct a `Bulkhead` from a name and a pre-built `BulkheadConfig`."""
        return cls(name, config=config)

    async def __aenter__(self) -> Self:
        """Admit the current task, waiting up to `max_wait` for a permit."""
        state = self._state
        semaphore = state.semaphore
        if semaphore is not None:
            wait = state.config.max_wait or 0.0
            try:
                async with asyncio.timeout(wait):
                    await semaphore.acquire()
            except TimeoutError:
                # A semaphore exists only when `max_concurrent` is set,
                # so the value is never `None` on this branch.
                raise BulkheadFullError(
                    name=self._name,
                    max_concurrent=state.config.max_concurrent,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                ) from None
        task = _current_task()
        stack = self._permits.get(task)
        if stack is None:
            self._permits[task] = [semaphore]
        else:
            stack.append(semaphore)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Release the permit acquired by the matching `__aenter__`."""
        task = _current_task()
        stack = self._permits[task]
        semaphore = stack.pop()
        if not stack:
            del self._permits[task]
        if semaphore is not None:
            semaphore.release()
        return None

    def __call__(
        self, fn: Callable[..., Awaitable[Any]], /
    ) -> Callable[..., Awaitable[Any]]:
        """Decorate ``fn`` so each call runs under this bulkhead."""
        if not iscoroutinefunction(fn):
            msg = (
                "Bulkhead only decorates async functions. Use "
                f"`bulkhead.to_thread(...)` for blocking work, got {fn!r}."
            )
            raise TypeError(msg)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            async with self:
                return await fn(*args, **kwargs)

        return async_wrapper

    async def to_thread(
        self,
        func: Annotated[
            Callable[..., Any],
            Doc("Blocking callable to run off the event loop."),
        ],
        /,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Run ``func`` in a worker thread, bounded by `max_workers`.

        Routes through the bulkhead's private `ThreadPoolExecutor` when
        `max_workers` is set, otherwise the event loop's shared executor
        (`asyncio.to_thread`).
        """
        max_workers = self._state.config.max_workers
        if max_workers is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"bulkhead-{self._name}",
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, functools.partial(func, *args, **kwargs)
        )

    async def _apply_reconfigure(self, new_config: BulkheadConfig) -> None:
        """Publish a fresh snapshot. In-flight calls keep their permit.

        A changed `max_concurrent` builds a new semaphore for calls that
        enter after the swap. A changed `max_workers` discards the
        private executor so the next `to_thread` rebuilds it.
        """
        if (
            self._executor is not None
            and new_config.max_workers != self._state.config.max_workers
        ):
            self._executor.shutdown(wait=False)
            self._executor = None
        self._state = _State(
            config=new_config, semaphore=_build_semaphore(new_config)
        )


def _build_semaphore(config: BulkheadConfig) -> asyncio.Semaphore | None:
    """Build a semaphore for the configured concurrency, or `None`."""
    if config.max_concurrent is None:
        return None
    return asyncio.Semaphore(config.max_concurrent)
