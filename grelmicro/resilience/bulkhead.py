"""Bulkhead."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from inspect import iscoroutinefunction
from threading import Lock as ThreadLock
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, NonNegativeFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._app import Grelmicro, _active_bulkhead, _current_micro
from grelmicro._component import Component, Usable, instantiate_if_class
from grelmicro._config import (
    Reconfigurable,
    env_prefixes,
    resolve_config,
)
from grelmicro._environment import (
    report_unmet_requirements,
    unmet_requirements,
)
from grelmicro.errors import OutOfContextError
from grelmicro.metrics import _emit
from grelmicro.resilience.errors import BulkheadFullError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from contextlib import AsyncExitStack
    from contextvars import Token
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
            "Maximum concurrent calls admitted to the bulkhead, per "
            "worker process. Four workers each admit this many, so the "
            "dependency sees four times it. `None` (the default) leaves "
            "concurrency unbounded."
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


@dataclass(slots=True)
class _Scope:
    """One app's record of the `uses=` items this bulkhead has opened."""

    lock: asyncio.Lock
    entered: int = 0
    opened: bool = False

    def forget(self) -> None:
        """Stop reporting the scope open, as its first item closes."""
        self.opened = False


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
            Doc(
                "Maximum concurrent calls, per worker process. `None` "
                "leaves it unbounded."
            ),
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
        uses: Annotated[
            Iterable[Usable | None],
            Doc(
                """
                Providers and Components, in the same shape as
                `Grelmicro(uses=[...])`, scoped to this bulkhead. Inside
                the scope, a Pattern that resolves its default backend
                (a bare `Lock("k")`, `cache.get(...)`, ...) picks up the
                matching Component here instead of the app's. A Pattern
                with an explicit `backend=` is unaffected. The bulkhead
                opens these on first entry and closes them when the app
                shuts down, so an active `Grelmicro` app is required. The
                scope belongs to that app, so a later app opens it again
                from the start. Entering once the app has shut down raises,
                and so does sharing the bulkhead with a second app running
                at the same time.
                A `None` entry is skipped, as in `Grelmicro(uses=[...])`.
                """
            ),
        ] = (),
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. Defaults to the "
                "process-wide `GREL_ENV_LOAD` flag. "
                "Pass False when the values here are the whole "
                "truth, because env reads fill every field not passed."
            ),
        ] = None,
    ) -> None:
        """Initialize the bulkhead."""
        env_prefix, kind_prefix = env_prefixes("BULKHEAD", name)
        self._setup(
            name,
            resolve_config(
                BulkheadConfig,
                explicit=None,
                kwargs={
                    "max_concurrent": max_concurrent,
                    "max_wait": max_wait,
                    "max_workers": max_workers,
                },
                env_prefix=env_prefix,
                kind_env_prefix=kind_prefix,
                env_load=env_load,
            ),
            uses,
        )
        self._track_reconfigure(env_prefix)

    def _setup(
        self,
        name: str,
        config: BulkheadConfig,
        uses: Iterable[Usable | None],
    ) -> None:
        """Wire the validated config and runtime state onto the instance."""
        self._name = name
        self._config = config
        self._state = _State(config=config, semaphore=_build_semaphore(config))
        self._reconfigure_lock = asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._uses = tuple(
            instantiate_if_class(item) for item in uses if item is not None
        )
        self._overrides: dict[tuple[str, str], Component] = {
            (item.kind, item.name): item
            for item in self._uses
            if isinstance(item, Component)
        }
        self._scoped_to: Grelmicro | None = None
        self._owner_lock = ThreadLock()
        self._scopes: dict[
            asyncio.Task[Any],
            list[tuple[asyncio.Semaphore | None, Token[Any] | None]],
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
        *,
        uses: Annotated[
            Iterable[Usable | None],
            Doc(
                "Providers and Components scoped to this bulkhead, in "
                "the same shape as on the constructor."
            ),
        ] = (),
    ) -> Self:
        """Construct a `Bulkhead` from a name and a pre-built `BulkheadConfig`.

        The config is taken as-is: no environment variable is read, and
        the instance is not registered for live reconfiguration.
        """
        instance = cls.__new__(cls)
        instance._setup(name, config, uses)  # noqa: SLF001
        return instance

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
                _emit.incr(
                    "grelmicro.bulkhead.rejections",
                    **{"bulkhead.name": self._name},
                )
                # A semaphore exists only when `max_concurrent` is set,
                # so the value is never `None` on this branch.
                raise BulkheadFullError(
                    name=self._name,
                    max_concurrent=state.config.max_concurrent,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                ) from None
        token: Token[Any] | None = None
        try:
            if self._uses:
                micro = _current_micro.get(None)
                if (
                    micro is None
                    or (scope := micro._scoped_uses.get(self)) is None  # noqa: SLF001
                    or not scope.opened
                ):
                    await self._open_uses()
            if self._overrides:
                current = _active_bulkhead.get(None)
                merged = (
                    {**current, **self._overrides}
                    if current
                    else dict(self._overrides)
                )
                token = _active_bulkhead.set(merged)
            task = _current_task()
        except BaseException:
            if token is not None:
                _active_bulkhead.reset(token)
            if semaphore is not None:
                semaphore.release()
            raise
        scope = (semaphore, token)
        stack = self._scopes.get(task)
        if stack is None:
            self._scopes[task] = [scope]
        else:
            stack.append(scope)
        _emit.add_up_down(
            "grelmicro.bulkhead.active", 1, **{"bulkhead.name": self._name}
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Release the permit and override scope from the matching `__aenter__`."""
        task = _current_task()
        stack = self._scopes[task]
        semaphore, token = stack.pop()
        if not stack:
            del self._scopes[task]
        if token is not None:
            _active_bulkhead.reset(token)
        if semaphore is not None:
            semaphore.release()
        _emit.add_up_down(
            "grelmicro.bulkhead.active", -1, **{"bulkhead.name": self._name}
        )
        return None

    async def _open_uses(self) -> None:
        """Open the `uses=` providers and components once per app.

        Entered in order so a Component borrows a provider opened just
        before it, and registered on the active app's exit stack, so they
        close when that app shuts down rather than per scope.

        The record of what is open lives on the app, so two apps that
        overlap each keep their own, and an app that shuts down takes its
        record with it. Within one app, an entry that failed part way
        resumes where it stopped rather than entering the same item twice.

        These components are not registered on the app, so the app cannot
        check their backend scope at startup. They are checked here instead,
        the first time the scope opens.
        """
        micro = Grelmicro.current()
        exit_stack = micro._exit_stack  # noqa: SLF001
        if exit_stack is None:
            raise OutOfContextError(self._no_scope_message(micro, None))
        scopes = micro._scoped_uses  # noqa: SLF001
        scope = scopes.get(self)
        if scope is None:
            # Nothing awaits between the lookup and the store, so two tasks
            # racing the first entry cannot both create a scope.
            scope = _Scope(lock=asyncio.Lock())
            scopes[self] = scope
        async with scope.lock:
            if scope.opened:
                return
            if self._lost_scope(micro, exit_stack):
                raise OutOfContextError(
                    self._no_scope_message(micro, exit_stack)
                )
            if self._claim_scope(micro):
                # Sits below every item, so it runs once they have all
                # closed and the next app is free to take the scope.
                exit_stack.callback(self._release_scope)
            report_unmet_requirements(
                unmet_requirements(self._uses), micro.environment
            )
            for item in self._uses[scope.entered :]:
                await item.__aenter__()
                if self._lost_scope(micro, exit_stack):
                    # The app moved on while this item was entering, so its
                    # stack will never close it. Close it here and leave the
                    # scope for the next app to open.
                    await item.__aexit__(None, None, None)
                    raise OutOfContextError(
                        self._no_scope_message(micro, exit_stack)
                    )
                exit_stack.push_async_exit(item)
                # Sits above the item, so the scope stops reporting itself
                # open before anything it holds closes.
                exit_stack.callback(scope.forget)
                scope.entered += 1
            scope.opened = True

    def _lost_scope(self, micro: Grelmicro, exit_stack: AsyncExitStack) -> bool:
        """Report whether the app moved on from the stack this open captured.

        A shutdown and a restart both land here, so an entry parked in an
        item never pushes onto a stack the app has finished with.
        """
        return micro._closing or micro._exit_stack is not exit_stack  # noqa: SLF001

    def _claim_scope(self, micro: Grelmicro) -> bool:
        """Take the `uses=` scope for `micro`, and report whether it was free.

        Synchronous on purpose. Nothing awaits between reading the owner and
        writing it, so two apps opening at once cannot both pass.

        Raises:
            OutOfContextError: If another live app holds the scope.
        """
        with self._owner_lock:
            owner = self._scoped_to
            if owner is None:
                self._scoped_to = micro
                return True
            if owner is not micro:
                raise OutOfContextError(self._shared_scope_message())
        return False

    def _release_scope(self) -> None:
        """Give the scope up once every item its app opened has closed."""
        with self._owner_lock:
            self._scoped_to = None

    def _shared_scope_message(self) -> str:
        """Describe a `uses=` scope another live app already holds."""
        return (
            f"Bulkhead {self._name!r} has its uses= scope open on another "
            "Grelmicro app. A bulkhead with uses= belongs to one app at a "
            "time, so give each app its own Bulkhead instance."
        )

    def _no_scope_message(
        self, micro: Grelmicro, exit_stack: AsyncExitStack | None
    ) -> str:
        """Describe a `uses=` scope the app cannot open right now."""
        if exit_stack is not None and micro._exit_stack is not exit_stack:  # noqa: SLF001
            return (
                f"Bulkhead {self._name!r} lost its uses= scope while opening "
                "it, because the app that owned it shut down. Enter it again "
                "under the running app."
            )
        if micro._closing:  # noqa: SLF001
            return (
                f"Bulkhead {self._name!r} was entered while its uses= scope "
                "was closing with the app that opened it. Enter it before "
                "the app shuts down, or list the provider in "
                "Grelmicro(uses=[...]) so it outlives the item that needs it."
            )
        return (
            f"Bulkhead {self._name!r} was entered before its app started. "
            "Enter it inside `async with micro:`, after startup has run."
        )

    def __call__[**P, R](
        self, fn: Callable[P, Awaitable[R]], /
    ) -> Callable[P, Awaitable[R]]:
        """Decorate ``fn`` so each call runs under this bulkhead."""
        if not iscoroutinefunction(fn):
            msg = (
                "Bulkhead only decorates async functions. Use "
                f"`bulkhead.to_thread(...)` for blocking work, got {fn!r}."
            )
            raise TypeError(msg)

        @functools.wraps(fn)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
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
