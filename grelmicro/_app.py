"""The Grelmicro app object."""

from __future__ import annotations

from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro._module import Module
from grelmicro.errors import GrelmicroError, OutOfContextError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from types import TracebackType

    from grelmicro.cache._module import Cache
    from grelmicro.sync._module import Sync
else:
    # Runtime fallback so `typing.get_type_hints(Grelmicro)` resolves the
    # `sync` / `cache` property annotations without forcing first-party
    # submodules to load at `import grelmicro`. Real types are visible to
    # static type checkers via the `TYPE_CHECKING` branch above.
    Cache = Any
    Sync = Any

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every item attached to the
    app (modules, task managers, health registries, custom async context
    managers, ...) and opens them as a single async context manager. Two
    `Grelmicro` instances in the same process are fully independent.

    The conventional variable name is `micro`:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.sync import Sync
    from grelmicro.task import Tasks

    tasks = Tasks()

    micro = Grelmicro(uses=[
        Sync(RedisSyncAdapter()),
        tasks,
    ])

    @tasks.interval(seconds=5)
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
        uses: Annotated[
            Iterable[AbstractAsyncContextManager[object]] | None,
            Doc(
                """
                Items registered at construction time. Equivalent to a
                sequence of `.use(item)` calls in the same order. Accepts
                both `Module` instances (registered with `(kind, name)`
                lookup, exposed on `micro.<kind>`) and plain async context
                managers (lifecycled only, caller holds the reference).
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the app and register any items passed at construction."""
        self._items: list[AbstractAsyncContextManager[object]] = []
        self._by_key: dict[tuple[str, str], Module] = {}
        self._by_kind: dict[str, Module] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._token: Any = None
        if uses is not None:
            for item in uses:
                self.use(item)

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

    def use(self, item: AbstractAsyncContextManager[object]) -> None:
        """Register an item to be lifecycled with the app.

        Three shapes are accepted:

        1. A `Module` instance: registered with `(kind, name)` lookup and
           exposed on `micro.<kind>`.
        2. A first-party backend (e.g. `RedisSyncAdapter`): auto-wrapped
           into its canonical `Module` (`Sync` for sync backends, `Cache`
           for cache backends) before registration.
        3. Any other async context manager: just lifecycled with the app,
           the caller keeps the reference.

        ```python
        # Auto-wrapped first-party backend
        micro.use(RedisSyncAdapter())          # registered as (sync, default)
        micro.use(RedisCacheAdapter())         # registered as (cache, default)

        # Explicit Module when a non-default name is needed
        micro.use(Sync(RedisSyncAdapter(), name="analytics"))

        # Plain async context manager: lifecycled only, caller holds reference
        tasks = Tasks()
        micro.use(tasks)
        ```

        Returns `None`. Mirrors FastAPI's `app.include_router(router)`
        pattern: pure side-effect registration. To access registered
        modules, use the typed `micro.sync` / `micro.cache` properties or
        `micro.get(kind, name)`. For plain async context managers, the
        caller already holds the reference.

        Raises:
            ModuleAlreadyRegisteredError: A different module is already
                registered under the same `(kind, name)` key. Plain async
                context managers do not raise; they are appended.
        """
        # Resolve the item to a Module if possible: pass-through for Module
        # instances, auto-wrap for first-party backends, None for plain CMs.
        module: Module | None = (
            item
            if isinstance(item, Module)
            else _maybe_wrap_first_party_backend(item)
        )
        if module is None:
            # Plain async context manager: lifecycle only, no kind/name lookup.
            self._items.append(item)
            return
        key = (module.kind, module.name)
        existing = self._by_key.get(key)
        if existing is module:
            return
        if existing is not None:
            msg = (
                f"module {key!r} is already registered. "
                f"Construct a new Grelmicro or pick a different name."
            )
            raise ModuleAlreadyRegisteredError(msg)
        self._by_key[key] = module
        # `micro.<kind>` prefers the entry named `"default"`. Only update the
        # kind-default index when this registration is the default one.
        # `__getattr__` falls back to the sole entry per kind when no default
        # is registered.
        if module.name == "default":
            self._by_kind[module.kind] = module
        self._items.append(module)

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
        """Swap module registrations for a block, restore them on exit.

        Used in tests to substitute mock modules:

        ```python
        async with micro:
            async with micro.override(Sync(MockSync())):
                await test_thing()
        ```

        The override is scoped to the surrounding `async with micro:` block.
        The new modules are entered when the override block opens and exited
        in reverse order when it closes.

        Plain async context managers (registered via `use(item)` without a
        `kind`) cannot be overridden through this method. The caller already
        holds the reference and can substitute a mock at construction time.

        Raises:
            OutOfContextError: If called outside an open `async with micro:`
                block. The override needs an active app to scope to.
        """
        if self._exit_stack is None:
            raise OutOfContextError(self, "override")
        snapshot_by_key = self._by_key.copy()
        snapshot_items = self._items.copy()
        snapshot_by_kind = self._by_kind.copy()
        async with AsyncExitStack() as stack:
            for module in modules:
                key = (module.kind, module.name)
                self._by_key[key] = module
                if module not in self._items:
                    self._items.append(module)
                if module.name == "default":
                    self._by_kind[module.kind] = module
                await stack.enter_async_context(module)
            try:
                yield
            finally:
                self._by_key = snapshot_by_key
                self._items = snapshot_items
                self._by_kind = snapshot_by_kind

    @property
    def sync(self) -> Sync:
        """The registered `Sync` module (default-named, or sole entry of kind `sync`)."""
        return self._resolve_kind("sync")

    @property
    def cache(self) -> Cache:
        """The registered `Cache` module (default-named, or sole entry of kind `cache`)."""
        return self._resolve_kind("cache")

    def _resolve_kind(self, name: str) -> Any:  # noqa: ANN401
        """Shared resolution logic for typed properties and `__getattr__`."""
        by_kind = self.__dict__.get("_by_kind", {})
        if name in by_kind:
            return by_kind[name]
        by_key = self.__dict__.get("_by_key", {})
        matches = [v for (k, _), v in by_key.items() if k == name]
        if len(matches) == 1:
            return matches[0]
        cls = type(self).__name__
        if matches:
            names = sorted(n for (k, n), _ in by_key.items() if k == name)
            msg = (
                f"{cls!r} has multiple {name!r} modules ({names}), "
                f"none named 'default'. Use micro.get({name!r}, <name>)."
            )
            raise AttributeError(msg)
        msg = f"{cls!r} object has no module of kind {name!r}"
        raise AttributeError(msg)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Resolve `micro.<kind>` to the registered module of that kind.

        Falls through to `_resolve_kind` (used by typed properties for
        first-party modules and by ad-hoc lookup for third-party modules).

        Resolution order, matching the legacy `BackendRegistry.get()` semantics:

        1. The module registered as `(kind, "default")` if present.
        2. The sole entry of that kind if exactly one is registered.
        3. Otherwise raises `AttributeError`.

        Returns `Any` so callers can invoke module-specific methods on
        third-party modules without per-call casts. First-party modules
        (`sync`, `cache`) are typed via dedicated properties.

        Use `micro.get(kind, name)` for explicit name-based resolution.
        """
        return self._resolve_kind(name)

    async def __aenter__(self) -> Self:
        """Open every registered item in registration order.

        The active-app `ContextVar` is set before entries so items can call
        `Grelmicro.current()` from their `__aenter__`. On partial-startup
        failure, the token is reset before unwinding.
        """
        if self._exit_stack is not None:
            raise OutOfContextError(self, "__aenter__")
        self._resolve_provider_sharing()
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        self._token = _current_micro.set(self)
        try:
            for item in self._items:
                await self._exit_stack.enter_async_context(item)
        except BaseException:
            _current_micro.reset(self._token)
            self._token = None
            await self._exit_stack.__aexit__(*_sys_exc_info_or_none())
            self._exit_stack = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close every item in reverse registration order (LIFO)."""
        if self._exit_stack is None:
            raise OutOfContextError(self, "__aexit__")
        try:
            # Keep `Grelmicro.current()` resolvable during teardown so items
            # that consult it from `__aexit__` still see the active app.
            return await self._exit_stack.__aexit__(exc_type, exc, tb)
        finally:
            if self._token is not None:
                _current_micro.reset(self._token)
                self._token = None
            self._exit_stack = None

    def _resolve_provider_sharing(self) -> None:
        """Dedupe implicitly-owned providers by `(class, env_prefix)`.

        Walks registered items in order. The first adapter that owns a
        provider keeps ownership and lifecycle responsibility. Later
        adapters with the same `(provider_class, env_prefix)` key are
        rebound to the same provider instance via `_rebind_provider`,
        so a single connection pool feeds every consumer.

        Adapters that received an explicit `provider=` instance are
        left alone: their lifecycle is the caller's responsibility.
        """
        cache: dict[tuple[type, str], object] = {}
        for item in self._items:
            target = getattr(item, "backend", item)
            if not getattr(target, "_owns_provider", False):
                continue
            provider = target._provider  # type: ignore[attr-defined]  # noqa: SLF001  # ty: ignore[unresolved-attribute]
            key = (type(provider), provider.env_prefix)
            shared = cache.get(key)
            if shared is None:
                cache[key] = provider
            elif shared is not provider:
                target._rebind_provider(shared)  # type: ignore[attr-defined]  # noqa: SLF001  # ty: ignore[unresolved-attribute]


def _sys_exc_info_or_none() -> tuple[Any, Any, Any]:
    """Return current exception triple (or three Nones if not in handler)."""
    import sys  # noqa: PLC0415

    return sys.exc_info()


def _maybe_wrap_first_party_backend(item: object) -> Module | None:
    """Wrap a first-party backend in its canonical Module, or return None.

    Imports are lazy so unused modules stay out of `import grelmicro`.
    The user importing `RedisCacheAdapter` already loads `grelmicro.cache`,
    so the lazy import here is a cache hit.
    """
    from grelmicro.cache._module import Cache  # noqa: PLC0415
    from grelmicro.cache._protocol import CacheBackend  # noqa: PLC0415
    from grelmicro.sync._module import Sync  # noqa: PLC0415
    from grelmicro.sync.abc import SyncBackend  # noqa: PLC0415

    if isinstance(item, CacheBackend):
        return Cache(item)
    if isinstance(item, SyncBackend):
        return Sync(item)
    return None


class ModuleAlreadyRegisteredError(GrelmicroError, RuntimeError):
    """Raised when registering a different module under an existing `(kind, name)` key."""


class ModuleNotRegisteredError(GrelmicroError, LookupError):
    """Raised when resolving a module that has not been registered."""


class NoActiveAppError(GrelmicroError, LookupError):
    """Raised by `Grelmicro.current()` when called outside any `async with micro:` block."""
