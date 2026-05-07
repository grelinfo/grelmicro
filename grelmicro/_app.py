"""The Grelmicro app object."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro._pattern import Pattern
from grelmicro.errors import GrelmicroError, OutOfContextError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from types import TracebackType

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")


def current_micro() -> Grelmicro:
    """Return the active `Grelmicro` app for the current asyncio task.

    Raises:
        NoActiveAppError: If no `Grelmicro` is open in the current task scope.
    """
    try:
        return _current_micro.get()
    except LookupError as exc:
        raise NoActiveAppError from exc


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every microservice pattern
    (sync, cache, task, health, ...) and opens them as a single async context
    manager. Two `Grelmicro` instances in the same process are fully
    independent.

    The conventional variable name is `micro`:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.task import Tasks

    micro = Grelmicro(patterns=[Tasks()])

    @micro.task.interval(seconds=5)
    async def cleanup(): ...

    async with micro:
        await asyncio.sleep(60)
    ```

    Inside the `async with micro:` block, primitives that omit an explicit
    `micro=` argument resolve through `current_micro()` (per asyncio task).

    Read more in the [Grelmicro app](architecture/grelmicro.md) docs.
    """

    def __init__(
        self,
        *,
        patterns: Annotated[
            Iterable[Pattern] | None,
            Doc(
                """
                Patterns registered at construction time. Equivalent to a
                sequence of `.use(pattern)` calls in the same order.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the app and register any patterns passed at construction."""
        self._patterns: list[Pattern] = []
        self._by_key: dict[tuple[str, str], Pattern] = {}
        self._by_kind: dict[str, Pattern] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._token: Any = None
        if patterns is not None:
            for pattern in patterns:
                self.use(pattern)

    def use[P: Pattern](self, pattern: P) -> P:
        """Register `pattern` and return it.

        The composite registration key is `(pattern.kind, pattern.name)`.
        Re-registering the same instance under the same key is a no-op.
        Registering a different instance under an existing key raises.

        Returns the registered pattern so the caller can keep a reference for
        later use:

        ```python
        tasks = micro.use(Tasks())
        tasks.add_task(my_task)
        ```

        Args:
            pattern: The pattern to register.

        Raises:
            PatternAlreadyRegisteredError: A different pattern is already
                registered under the same `(kind, name)` key.
        """
        key = (pattern.kind, pattern.name)
        existing = self._by_key.get(key)
        if existing is pattern:
            return pattern
        if existing is not None:
            msg = (
                f"pattern {key!r} is already registered. "
                f"Construct a new Grelmicro or pick a different name."
            )
            raise PatternAlreadyRegisteredError(msg)
        self._by_key[key] = pattern
        self._patterns.append(pattern)
        # Last-write-wins for `micro.<kind>` resolved through `__getattr__`,
        # mirroring the registry's default-name fallback when only one entry
        # exists per kind.
        self._by_kind[pattern.kind] = pattern
        return pattern

    def get(self, kind: str, name: str = "default") -> Pattern:
        """Resolve a registered pattern by `(kind, name)`.

        Raises:
            PatternNotRegisteredError: If no pattern matches.
        """
        try:
            return self._by_key[(kind, name)]
        except KeyError as exc:
            msg = f"no pattern registered for {(kind, name)!r}."
            raise PatternNotRegisteredError(msg) from exc

    @asynccontextmanager
    async def override(
        self,
        *patterns: Annotated[
            Pattern,
            Doc(
                """
                Patterns to install for the duration of the block. Each one
                shadows any pattern already registered under the same
                `(kind, name)` key. Original registrations are restored on
                exit, even if the block raises.
                """,
            ),
        ],
    ) -> AsyncIterator[None]:
        """Swap registrations for a block, restore them on exit.

        Used in tests to substitute mock patterns:

        ```python
        async with micro.override(Sync(MockSync())):
            await test_thing()
        ```

        The override is scoped to the surrounding `async with micro:` block.
        The new patterns are entered when the override block opens and exited
        in reverse order when it closes.
        """
        snapshot_by_key = self._by_key.copy()
        snapshot_patterns = self._patterns.copy()
        snapshot_by_kind = self._by_kind.copy()
        async with AsyncExitStack() as stack:
            for pattern in patterns:
                key = (pattern.kind, pattern.name)
                self._by_key[key] = pattern
                if pattern not in self._patterns:
                    self._patterns.append(pattern)
                self._by_kind[pattern.kind] = pattern
                await stack.enter_async_context(pattern)
            try:
                yield
            finally:
                self._by_key = snapshot_by_key
                self._patterns = snapshot_patterns
                self._by_kind = snapshot_by_kind

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Resolve `micro.<kind>` to the most recently registered pattern of that kind.

        Returns `Any` so callers can invoke pattern-specific methods
        (`micro.task.interval(...)`, `micro.cache.get(...)`) without per-call
        casts. The actual concrete type depends on the registered pattern.
        """
        try:
            return self.__dict__["_by_kind"][name]
        except KeyError:
            msg = f"{type(self).__name__!r} object has no pattern of kind {name!r}"
            raise AttributeError(msg) from None

    async def __aenter__(self) -> Self:
        """Open every registered pattern in registration order."""
        if self._exit_stack is not None:
            raise OutOfContextError(self, "__aenter__")
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        try:
            for pattern in self._patterns:
                await self._exit_stack.enter_async_context(pattern)
        except BaseException:
            await self._exit_stack.__aexit__(*_sys_exc_info_or_none())
            self._exit_stack = None
            raise
        self._token = _current_micro.set(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close every pattern in reverse registration order (LIFO)."""
        if self._exit_stack is None:
            raise OutOfContextError(self, "__aexit__")
        if self._token is not None:
            _current_micro.reset(self._token)
            self._token = None
        result = await self._exit_stack.__aexit__(exc_type, exc, tb)
        self._exit_stack = None
        return result


def _sys_exc_info_or_none() -> tuple[Any, Any, Any]:
    """Return current exception triple (or three Nones if not in handler)."""
    import sys  # noqa: PLC0415

    return sys.exc_info()


class PatternAlreadyRegisteredError(GrelmicroError, RuntimeError):
    """Raised when registering a different pattern under an existing `(kind, name)` key."""


class PatternNotRegisteredError(GrelmicroError, LookupError):
    """Raised when resolving a pattern that has not been registered."""


class NoActiveAppError(GrelmicroError, LookupError):
    """Raised by `current_micro()` when called outside any `async with micro:` block."""
