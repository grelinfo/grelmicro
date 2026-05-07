"""The Grelmicro app object."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro._module import Module
from grelmicro.errors import GrelmicroError, OutOfContextError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from types import TracebackType

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every module (sync, cache,
    task, health, ...) and opens them as a single async context manager. Two
    `Grelmicro` instances in the same process are fully independent.

    The conventional variable name is `micro`:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.task import Tasks

    micro = Grelmicro(modules=[Tasks()])

    @micro.task.interval(seconds=5)
    async def cleanup(): ...

    async with micro:
        await asyncio.sleep(60)
    ```

    Inside the `async with micro:` block, primitives that omit an explicit
    `micro=` argument resolve through `Grelmicro.current()` (per asyncio task).

    Read more in the [Grelmicro app](architecture/grelmicro.md) docs.
    """

    def __init__(
        self,
        *,
        modules: Annotated[
            Iterable[Module] | None,
            Doc(
                """
                Modules registered at construction time. Equivalent to a
                sequence of `.use(module)` calls in the same order.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the app and register any modules passed at construction."""
        self._modules: list[Module] = []
        self._by_key: dict[tuple[str, str], Module] = {}
        self._by_kind: dict[str, Module] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._token: Any = None
        if modules is not None:
            for module in modules:
                self.use(module)

    @classmethod
    def current(cls) -> Grelmicro:
        """Return the active `Grelmicro` app for the current asyncio task.

        Use inside an `async with micro:` block to look up the active app:

        ```python
        from grelmicro import Grelmicro

        micro = Grelmicro.current()
        ```

        The lookup is per asyncio task, so concurrent tasks each see their
        own active `Grelmicro`.

        Raises:
            NoActiveAppError: If called outside any `async with micro:`
                block in the current task scope.
        """
        try:
            return _current_micro.get()
        except LookupError as exc:
            raise NoActiveAppError from exc

    def use[M: Module](self, module: M) -> M:
        """Register `module` and return it.

        The composite registration key is `(module.kind, module.name)`.
        Re-registering the same instance under the same key is a no-op.
        Registering a different instance under an existing key raises.

        Returns the registered module so the caller can keep a reference for
        later use:

        ```python
        tasks = micro.use(Tasks())
        tasks.add_task(my_task)
        ```

        Args:
            module: The module to register.

        Raises:
            ModuleAlreadyRegisteredError: A different module is already
                registered under the same `(kind, name)` key.
        """
        key = (module.kind, module.name)
        existing = self._by_key.get(key)
        if existing is module:
            return module
        if existing is not None:
            msg = (
                f"module {key!r} is already registered. "
                f"Construct a new Grelmicro or pick a different name."
            )
            raise ModuleAlreadyRegisteredError(msg)
        self._by_key[key] = module
        self._modules.append(module)
        # Last-write-wins for `micro.<kind>` resolved through `__getattr__`,
        # mirroring the registry's default-name fallback when only one entry
        # exists per kind.
        self._by_kind[module.kind] = module
        return module

    def get(self, kind: str, name: str = "default") -> Any:  # noqa: ANN401
        """Resolve a registered module by `(kind, name)`.

        Returns `Any` for the same reason `micro.<kind>` does: the dynamic
        registration can't be statically typed without a global registry.
        Callers know the concrete type they registered.

        Raises:
            ModuleNotRegisteredError: If no module matches.
        """
        try:
            return self._by_key[(kind, name)]
        except KeyError as exc:
            msg = f"no module registered for {(kind, name)!r}."
            raise ModuleNotRegisteredError(msg) from exc

    @asynccontextmanager
    async def override(
        self,
        *modules: Annotated[
            Module,
            Doc(
                """
                Modules to install for the duration of the block. Each one
                shadows any module already registered under the same
                `(kind, name)` key. Original registrations are restored on
                exit, even if the block raises.
                """,
            ),
        ],
    ) -> AsyncIterator[None]:
        """Swap registrations for a block, restore them on exit.

        Used in tests to substitute mock modules:

        ```python
        async with micro.override(Sync(MockSync())):
            await test_thing()
        ```

        The override is scoped to the surrounding `async with micro:` block.
        The new modules are entered when the override block opens and exited
        in reverse order when it closes.

        Raises:
            OutOfContextError: If called outside an open `async with micro:`
                block. The override needs an active app to scope to.
        """
        if self._exit_stack is None:
            raise OutOfContextError(self, "override")
        snapshot_by_key = self._by_key.copy()
        snapshot_modules = self._modules.copy()
        snapshot_by_kind = self._by_kind.copy()
        async with AsyncExitStack() as stack:
            for module in modules:
                key = (module.kind, module.name)
                self._by_key[key] = module
                if module not in self._modules:
                    self._modules.append(module)
                self._by_kind[module.kind] = module
                await stack.enter_async_context(module)
            try:
                yield
            finally:
                self._by_key = snapshot_by_key
                self._modules = snapshot_modules
                self._by_kind = snapshot_by_kind

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Resolve `micro.<kind>` to the most recently registered module of that kind.

        Returns `Any` so callers can invoke module-specific methods
        (`micro.task.interval(...)`, `micro.cache.get(...)`) without per-call
        casts. The actual concrete type depends on the registered module.
        """
        try:
            return self.__dict__["_by_kind"][name]
        except KeyError:
            msg = (
                f"{type(self).__name__!r} object has no module of kind {name!r}"
            )
            raise AttributeError(msg) from None

    async def __aenter__(self) -> Self:
        """Open every registered module in registration order."""
        if self._exit_stack is not None:
            raise OutOfContextError(self, "__aenter__")
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        try:
            for module in self._modules:
                await self._exit_stack.enter_async_context(module)
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
        """Close every module in reverse registration order (LIFO)."""
        if self._exit_stack is None:
            raise OutOfContextError(self, "__aexit__")
        try:
            # Keep `Grelmicro.current()` resolvable during teardown so modules
            # that consult it from `__aexit__` still see the active app.
            return await self._exit_stack.__aexit__(exc_type, exc, tb)
        finally:
            if self._token is not None:
                _current_micro.reset(self._token)
                self._token = None
            self._exit_stack = None


def _sys_exc_info_or_none() -> tuple[Any, Any, Any]:
    """Return current exception triple (or three Nones if not in handler)."""
    import sys  # noqa: PLC0415

    return sys.exc_info()


class ModuleAlreadyRegisteredError(GrelmicroError, RuntimeError):
    """Raised when registering a different module under an existing `(kind, name)` key."""


class ModuleNotRegisteredError(GrelmicroError, LookupError):
    """Raised when resolving a module that has not been registered."""


class NoActiveAppError(GrelmicroError, LookupError):
    """Raised by `Grelmicro.current()` when called outside any `async with micro:` block."""
