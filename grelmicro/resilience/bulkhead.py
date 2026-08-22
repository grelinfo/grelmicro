"""Bulkhead."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from inspect import iscoroutinefunction
from threading import Lock as ThreadLock
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, NonNegativeFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._app import (
    AmbiguousProviderError,
    Grelmicro,
    NoActiveAppError,
    _active_bulkhead,
    _current_micro,
    _default_components_for_provider,
    _maybe_wrap_first_party_backend,
)
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
from grelmicro.providers._base import Provider
from grelmicro.resilience.errors import BulkheadFullError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from contextlib import AbstractAsyncContextManager, AsyncExitStack
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


@dataclass(slots=True, eq=False)
class _Scope:
    """One app run's record of the `uses=` items this bulkhead has opened."""

    lock: asyncio.Lock
    entered: int = 0
    opened: bool = False
    release_armed: bool = False
    drop_armed: bool = False
    borrowed_from: _Scope | None = None
    borrowers: set[_Scope] = field(default_factory=set)


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
                scope belongs to that app run, so a later run opens it
                again from the start. A run overlapping the one that owns
                the scope borrows it, and closes with it. An entry that
                finds no open scope, and cannot open one, raises
                `OutOfContextError`.
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
        self._uses = _expand_uses(name, uses)
        _check_usable(name, self._uses)
        self._overrides: dict[tuple[str, str], Component] = {
            (item.kind, item.name): item
            for item in self._uses
            if isinstance(item, Component)
        }
        self._scoped_to: Grelmicro | None = None
        self._scope_stack: AsyncExitStack | None = None
        self._opening = 0
        self._unwound = False
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
                # Falls back to the run holding the scope when this context
                # carries no binding. Ownership is exclusive, so that run is
                # the only one it can be.
                micro = _current_micro.get(self._scoped_to)
                if (
                    micro is None
                    or (record := micro._scoped_uses.get(self)) is None  # noqa: SLF001
                    or not record.opened
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
        micro = _current_micro.get(self._scoped_to)
        if micro is None:
            raise NoActiveAppError(self._no_app_message())
        exit_stack = micro._exit_stack  # noqa: SLF001
        if exit_stack is None:
            raise OutOfContextError(self._no_scope_message(micro, None))
        if self._borrows_open_scope(micro, exit_stack):
            return
        scope = self._scope_for(micro)
        async with scope.lock:
            if scope.opened:
                return
            if self._lost_scope(micro, exit_stack):
                raise OutOfContextError(
                    self._no_scope_message(micro, exit_stack)
                )
            if self._claim_scope(micro, exit_stack) and not scope.release_armed:
                # Sits below every item, so it runs once they have all
                # closed and the next run is free to take the scope. Armed
                # once per run, because a claim given back and taken again
                # would otherwise stack one of these per attempt.
                exit_stack.callback(self._release_scope, exit_stack)
                scope.release_armed = True
            try:
                await self._enter_uses(micro, exit_stack, scope)
            except BaseException:
                self._drop_empty_claim(scope, exit_stack)
                raise

    async def _enter_uses(
        self, micro: Grelmicro, exit_stack: AsyncExitStack, scope: _Scope
    ) -> None:
        """Enter what the scope has left to enter, in order."""
        report_unmet_requirements(
            unmet_requirements(self._uses), micro.environment
        )
        for item in self._uses[scope.entered :]:
            # Counted for as long as the entry is in flight. The exit
            # stack does not know about an item still being entered, so
            # this is what keeps the scope from changing hands under it.
            with self._owner_lock:
                self._opening += 1
            try:
                await item.__aenter__()
                if self._lost_scope(micro, exit_stack):
                    # The app moved on while this item was entering, so
                    # its stack will never close it. Close it here and
                    # leave the scope for the next app to open.
                    msg = self._no_scope_message(micro, exit_stack)
                    try:
                        await item.__aexit__(None, None, None)
                    except Exception as exc:
                        raise OutOfContextError(msg) from exc
                    raise OutOfContextError(msg)
                exit_stack.push_async_exit(item)
                scope.entered += 1
            finally:
                self._finish_open()
        # Sits above every item, so the scope stops reporting itself open
        # before anything it holds closes. Pushed once the last item is on
        # the stack, because anything pushed after it would close first.
        exit_stack.callback(self._forget_scope, scope)
        scope.opened = True

    def _lost_scope(self, micro: Grelmicro, exit_stack: AsyncExitStack) -> bool:
        """Report whether the app moved on from the stack this open captured.

        A shutdown and a restart both land here, so an entry parked in an
        item never pushes onto a stack the app has finished with.
        """
        return micro._closing or micro._exit_stack is not exit_stack  # noqa: SLF001

    def _scope_for(self, micro: Grelmicro) -> _Scope:
        """Return this run's record of the scope, starting one if it is new.

        Nothing awaits between the lookup and the store, so two tasks racing
        the first entry cannot both start a record.
        """
        scopes = micro._scoped_uses  # noqa: SLF001
        scope = scopes.get(self)
        if scope is None:
            scope = _Scope(lock=asyncio.Lock())
            scopes[self] = scope
        return scope

    def _borrows_open_scope(
        self, micro: Grelmicro, exit_stack: AsyncExitStack
    ) -> bool:
        """Report whether another run holds this scope open for `micro`.

        Only the run that owns a scope enters its items, so a run that
        overlaps it borrows what is open rather than opening a second set
        that the first to exit would close under the other.

        Raises:
            OutOfContextError: If that run holds the scope but not open,
                because it is still opening it or already closing it.
        """
        with self._owner_lock:
            holder = self._scoped_to
            if holder is None or holder is micro:
                return False
            held = holder._scoped_uses.get(self)  # noqa: SLF001
            if held is None or not held.opened:
                raise OutOfContextError(self._shared_scope_message(micro))
        # Checked against this run's environment, not the owner's, and before
        # anything is recorded, so a check that fails records nothing.
        report_unmet_requirements(
            unmet_requirements(self._uses), micro.environment
        )
        # Recorded on this run so the check runs once rather than per entry.
        # The same record is reused each time this run borrows, so borrowing
        # again after the owner changed leaves nothing of the last one.
        borrowed = self._scope_for(micro)
        with self._owner_lock:
            if not held.opened:
                raise OutOfContextError(self._shared_scope_message(micro))
            # Whichever scope this run last borrowed from already let go:
            # the owner clears the link as it forgets, and this run's own
            # drop clears it too, so there is never a second one to unpick.
            borrowed.opened = True
            borrowed.borrowed_from = held
            held.borrowers.add(borrowed)
            armed = borrowed.drop_armed
            borrowed.drop_armed = True
        if not armed:
            # Dropped when this run ends, so a long-lived owner does not
            # collect a record per run that ever borrowed from it.
            exit_stack.callback(self._drop_borrow, borrowed)
        return True

    def _drop_borrow(self, borrowed: _Scope) -> None:
        """Take this run's borrow off whatever scope it borrowed from.

        Stops reporting the scope open as well, because the owner can no
        longer reach this record to forget it, and clears the arming so a
        borrow taken after this still registers its own drop.
        """
        with self._owner_lock:
            held = borrowed.borrowed_from
            if held is not None:
                held.borrowers.discard(borrowed)
                borrowed.borrowed_from = None
            borrowed.opened = False
            borrowed.drop_armed = False

    def _forget_scope(self, scope: _Scope) -> None:
        """Stop reporting the scope open, before anything it holds closes."""
        with self._owner_lock:
            scope.opened = False
            for borrower in scope.borrowers:
                borrower.opened = False
                borrower.borrowed_from = None
            scope.borrowers.clear()

    def _claim_scope(
        self, micro: Grelmicro, exit_stack: AsyncExitStack
    ) -> bool:
        """Take the `uses=` scope for one app run, and report if it was free.

        Held against the exit stack rather than the app, because one app
        reused across runs is one app object and several scopes.

        Synchronous on purpose. Nothing awaits between reading the holder
        and writing it, so two runs opening at once cannot both pass.

        Raises:
            OutOfContextError: If another app run still holds the scope.
        """
        with self._owner_lock:
            held = self._scope_stack
            if held is None:
                self._scope_stack = exit_stack
                self._scoped_to = micro
                self._unwound = False
                return True
            if held is not exit_stack:
                raise OutOfContextError(self._shared_scope_message(micro))
        return False

    def _release_scope(self, exit_stack: AsyncExitStack) -> None:
        """Give the scope up once every item its run opened has closed.

        Does nothing once the run has already given the scope back, so a
        claim dropped early cannot free the run that took it next.
        """
        with self._owner_lock:
            if self._scope_stack is not exit_stack:
                return
            self._unwound = True
            self._free_scope()

    def _drop_empty_claim(
        self, scope: _Scope, exit_stack: AsyncExitStack
    ) -> None:
        """Give the scope back when an open left nothing entered behind it.

        An open that fails before its first item holds nothing, so keeping
        the claim would refuse every other run until this one shuts down.
        """
        if scope.entered:
            return
        with self._owner_lock:
            if self._scope_stack is exit_stack:
                self._scope_stack = None
                self._scoped_to = None
                self._unwound = False

    def _finish_open(self) -> None:
        """Drop one in-flight entry, and the scope with the last of them."""
        with self._owner_lock:
            self._opening -= 1
            self._free_scope()

    def _free_scope(self) -> None:
        """Hand the scope back once nothing holds it. Call under the lock."""
        if self._unwound and not self._opening:
            self._scope_stack = None
            self._scoped_to = None
            self._unwound = False

    def _no_app_message(self) -> str:
        """Describe a `uses=` bulkhead entered with no app to open it under."""
        return (
            f"Bulkhead {self._name!r} was entered with no active Grelmicro "
            "app. Its uses= scope needs one, so enter it inside "
            "`async with micro:`."
        )

    def _shared_scope_message(self, micro: Grelmicro) -> str:
        """Describe a `uses=` scope another app run already holds."""
        holder = self._scoped_to
        if holder is micro:
            return (
                f"Bulkhead {self._name!r} still has its uses= scope open "
                "from an earlier run of this app, which has not finished "
                "closing it. Enter it again once that run has drained."
            )
        if self._unwound:
            return (
                f"Bulkhead {self._name!r} still has its uses= scope held by "
                "a run that has shut down, because one of its uses= items is "
                "still being entered. That entry has to finish first."
            )
        if holder is not None and holder._closing:  # noqa: SLF001
            return (
                f"Bulkhead {self._name!r} is closing its uses= scope with "
                "the app run that owns it. Enter it again once that run has "
                "finished, and this one opens the scope for itself."
            )
        if self._opening:
            return (
                f"Bulkhead {self._name!r} is still opening its uses= scope "
                "on the app run that owns it. Enter it again once that has "
                "finished."
            )
        return (
            f"Bulkhead {self._name!r} has an unfinished uses= scope on the "
            "app run that owns it, because opening it stopped part way. "
            "That run has to finish opening it."
        )

    def _no_scope_message(
        self, micro: Grelmicro, exit_stack: AsyncExitStack | None
    ) -> str:
        """Describe a `uses=` scope the app cannot open right now."""
        current = micro._exit_stack  # noqa: SLF001
        if (
            exit_stack is not None
            and current is not None
            and current is not exit_stack
        ):
            return (
                f"Bulkhead {self._name!r} lost its uses= scope while opening "
                "it, because the app that owned it shut down. Enter it again "
                "under the running app."
            )
        if micro._closing and current is not None:  # noqa: SLF001
            return (
                f"Bulkhead {self._name!r} was entered while its uses= scope "
                "was closing with the app run that owns it. Enter it before "
                "that run shuts down, or list the provider in "
                "Grelmicro(uses=[...]) so it outlives the item that needs it."
            )
        if micro._closing:  # noqa: SLF001
            return (
                f"Bulkhead {self._name!r} was entered after the app run that "
                "opened its uses= scope had shut down. Enter it under a "
                "running app."
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


def _expand_uses(
    name: str, uses: Iterable[Usable | None]
) -> tuple[AbstractAsyncContextManager[object], ...]:
    """Resolve `uses=` the way `Grelmicro(uses=[...])` resolves it.

    A bare backend becomes its Component. One bare Provider keeps its own
    lifecycle and gains a default Component for every kind it serves that no
    Component in the list already claims, so the shortest form
    (`uses=[redis]`) scopes what it can serve instead of overriding nothing.

    Two or more bare Providers fill no defaults, because neither can be the
    default for a kind they both serve.

    Raises:
        AmbiguousProviderError: Two or more bare Providers are listed with no
            Component, so the default for each kind is ambiguous.
    """
    items = [
        _resolve_usable(instantiate_if_class(item))
        for item in uses
        if item is not None
    ]
    providers = [item for item in items if isinstance(item, Provider)]
    claimed = {item.kind for item in items if isinstance(item, Component)}
    if len(providers) > 1:
        if not claimed:
            raise AmbiguousProviderError(_ambiguous_message(name, providers))
        return tuple(items)
    resolved: list[AbstractAsyncContextManager[object]] = []
    for item in items:
        resolved.append(item)
        if not isinstance(item, Provider):
            continue
        resolved.extend(
            component
            for component in _default_components_for_provider(item)
            if component.kind not in claimed
        )
    return tuple(resolved)


def _ambiguous_message(name: str, providers: list[Provider]) -> str:
    """Describe a `uses=` list no single Provider can fill the defaults of."""
    listed = ", ".join(type(provider).__name__ for provider in providers)
    return (
        f"Bulkhead {name!r} lists multiple providers ({listed}) in uses= with "
        f"no components, so the default component for each kind is ambiguous. "
        f"Wrap each provider in the components it should serve, for example "
        f"Cache(provider) or RateLimiterComponent(provider)."
    )


def _resolve_usable[T](item: T) -> T | Component:
    """Wrap a bare first-party backend in its Component, as the app does.

    `uses=` takes the same shape as `Grelmicro(uses=[...])`, so a bare
    `MemoryLockAdapter()` has to become the `Coordination` that a Pattern
    can resolve against, rather than opening as an item that overrides
    nothing.
    """
    if isinstance(item, Component | Provider):
        return item
    wrapped = _maybe_wrap_first_party_backend(item)
    return item if wrapped is None else wrapped


def _check_usable(name: str, items: tuple[Usable, ...]) -> None:
    """Refuse a `uses=` entry that does not carry the async context protocol.

    Checks the same names on the type that `async with` resolves. An entry
    that carries them and still cannot be awaited is left to fail on open,
    because only calling it would tell them apart.
    """
    for item in items:
        kind = type(item)
        if hasattr(kind, "__aenter__") and hasattr(kind, "__aexit__"):
            continue
        msg = (
            f"Bulkhead {name!r} got a {kind.__name__} in uses=, which "
            "is not an async context manager. Pass a Provider, a Component, "
            "or an object with __aenter__ and __aexit__."
        )
        raise TypeError(msg)


def _build_semaphore(config: BulkheadConfig) -> asyncio.Semaphore | None:
    """Build a semaphore for the configured concurrency, or `None`."""
    if config.max_concurrent is None:
        return None
    return asyncio.Semaphore(config.max_concurrent)
