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

from grelmicro._component import Component
from grelmicro.errors import GrelmicroError, OutOfContextError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from types import TracebackType

    from grelmicro.cache._component import Cache
    from grelmicro.log._component import Log
    from grelmicro.sync._component import Sync
    from grelmicro.trace._component import Trace
else:
    # Runtime fallback so `typing.get_type_hints(Grelmicro)` resolves the
    # `sync` / `cache` property annotations without forcing first-party
    # submodules to load at `import grelmicro`. Real types are visible to
    # static type checkers via the `TYPE_CHECKING` branch above.
    Cache = Any
    Log = Any
    Sync = Any
    Trace = Any

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every item attached to the
    app (components, task managers, health registries, custom async context
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
                both `Component` instances (registered with `(kind, name)`
                lookup, exposed on `micro.<kind>`) and plain async context
                managers (lifecycled only, caller holds the reference).
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the app and register any items passed at construction."""
        self._items: list[AbstractAsyncContextManager[object]] = []
        self._by_key: dict[tuple[str, str], Component] = {}
        self._by_kind: dict[str, Component] = {}
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

    def use(
        self,
        item: Annotated[
            AbstractAsyncContextManager[object],
            Doc(
                """
                The item to register and lifecycle with the app. A `Component`
                instance is indexed under `(kind, name)` and exposed on
                `micro.<kind>`. A first-party backend is auto-wrapped into its
                matching `Component`. Any other async context manager is just
                lifecycled, and the caller keeps the reference.
                """,
            ),
        ],
    ) -> None:
        """Register an item to be lifecycled with the app.

        Three shapes are accepted:

        1. A `Component` instance: registered with `(kind, name)` lookup and
           exposed on `micro.<kind>`.
        2. A first-party backend (e.g. `RedisSyncAdapter`): auto-wrapped
           into the matching `Component` (`Sync` for sync backends, `Cache`
           for cache backends) before registration.
        3. Any other async context manager: just lifecycled with the app,
           the caller keeps the reference.

        ```python
        # Auto-wrapped first-party backend
        micro.use(RedisSyncAdapter())          # registered as (sync, default)
        micro.use(RedisCacheAdapter())         # registered as (cache, default)

        # Explicit Component when a non-default name is needed
        micro.use(Sync(RedisSyncAdapter(), name="analytics"))

        # Plain async context manager: lifecycled only, caller holds reference
        tasks = Tasks()
        micro.use(tasks)
        ```

        Returns `None`. Mirrors FastAPI's `app.include_router(router)`
        pattern: pure side-effect registration. To access registered
        components, use the typed `micro.sync` / `micro.cache` properties or
        `micro.get(kind, name)`. For plain async context managers, the
        caller already holds the reference.

        Raises:
            ComponentAlreadyRegisteredError: A different component is already
                registered under the same `(kind, name)` key. Plain async
                context managers do not raise; they are appended.
        """
        # Resolve the item to a Component if possible: pass-through for Component
        # instances, auto-wrap for first-party backends, None for plain CMs.
        component: Component | None = (
            item
            if isinstance(item, Component)
            else _maybe_wrap_first_party_backend(item)
        )
        if component is None:
            # Plain async context manager: lifecycle only, no kind/name lookup.
            self._items.append(item)
            return
        key = (component.kind, component.name)
        existing = self._by_key.get(key)
        if existing is component:
            return
        if existing is not None:
            msg = (
                f"component {key!r} is already registered. "
                f"Construct a new Grelmicro or pick a different name."
            )
            raise ComponentAlreadyRegisteredError(msg)
        self._by_key[key] = component
        # `micro.<kind>` prefers the entry named `"default"`. Only update the
        # kind-default index when this registration is the default one.
        # `__getattr__` falls back to the sole entry per kind when no default
        # is registered.
        if component.name == "default":
            self._by_kind[component.kind] = component
        self._items.append(component)

    def get(
        self,
        kind: Annotated[
            str,
            Doc(
                """
                Component category, matching the `kind` class attribute on
                the registered component (`"sync"`, `"cache"`, `"ratelimiter"`,
                `"circuitbreaker"`, `"log"`, `"trace"`, `"tasks"`, `"health"`).
                """,
            ),
        ],
        name: Annotated[
            str,
            Doc(
                """
                Component instance name. `"default"` matches the entry that
                also backs `micro.<kind>`. Pass the explicit name to resolve
                a secondary registration such as
                `Sync(backend, name="analytics")`.
                """,
            ),
        ] = "default",
    ) -> Any:  # noqa: ANN401
        """Resolve a registered component by `(kind, name)`.

        Returns `Any` for the same reason `micro.<kind>` does: the dynamic
        registration can't be statically typed without a global registry.
        Callers know the concrete type they registered.

        Raises:
            ComponentNotRegisteredError: If no component matches.
        """
        try:
            return self._by_key[(kind, name)]
        except KeyError as exc:
            registered = sorted(self._by_key)
            if registered:
                hint = "registered: " + ", ".join(repr(k) for k in registered)
            else:
                hint = "no components are registered"
            msg = f"no component registered for {(kind, name)!r}. {hint}."
            raise ComponentNotRegisteredError(msg) from exc

    @asynccontextmanager
    async def override(
        self,
        *components: Annotated[
            Component,
            Doc(
                """
                Components to install for the duration of the block. Each one
                shadows any component already registered under the same
                `(kind, name)` key. Original registrations are restored on
                exit, even if the block raises.
                """,
            ),
        ],
    ) -> AsyncIterator[None]:
        """Swap component registrations for a block, restore them on exit.

        Used in tests to substitute mock components:

        ```python
        async with micro:
            async with micro.override(Sync(MockSync())):
                await test_thing()
        ```

        The override is scoped to the surrounding `async with micro:` block.
        The new components are entered when the override block opens and exited
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
            for component in components:
                key = (component.kind, component.name)
                self._by_key[key] = component
                if component not in self._items:  # pragma: no branch
                    self._items.append(component)
                if component.name == "default":  # pragma: no branch
                    self._by_kind[component.kind] = component
                await stack.enter_async_context(component)
            try:
                yield
            finally:
                self._by_key = snapshot_by_key
                self._items = snapshot_items
                self._by_kind = snapshot_by_kind

    @property
    def components(self) -> tuple[Component, ...]:
        """Registered `Component` instances in registration order.

        Plain async context managers passed to `use(...)` are not included.
        Useful for `/healthz`-style introspection that prints what is wired
        up on the running app.
        """
        return tuple(self._by_key.values())

    @property
    def sync(self) -> Sync:
        """The registered `Sync` component (default-named, or sole entry of kind `sync`)."""
        return self._resolve_kind("sync")

    @property
    def cache(self) -> Cache:
        """The registered `Cache` component (default-named, or sole entry of kind `cache`)."""
        return self._resolve_kind("cache")

    @property
    def log(self) -> Log:
        """The registered `Log` component (default-named, or sole entry of kind `log`)."""
        return self._resolve_kind("log")

    @property
    def trace(self) -> Trace:
        """The registered `Trace` component (default-named, or sole entry of kind `trace`)."""
        return self._resolve_kind("trace")

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
                f"{cls!r} has multiple {name!r} components ({names}), "
                f"none named 'default'. Use micro.get({name!r}, <name>)."
            )
            raise AttributeError(msg)
        msg = f"{cls!r} object has no component of kind {name!r}"
        raise AttributeError(msg)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Resolve `micro.<kind>` to the registered component of that kind.

        Falls through to `_resolve_kind` (used by typed properties for
        first-party components and by ad-hoc lookup for third-party components).

        Resolution order:

        1. The component registered as `(kind, "default")` if present.
        2. The sole entry of that kind if exactly one is registered.
        3. Otherwise raises `AttributeError`.

        Returns `Any` so callers can invoke component-specific methods on
        third-party components without per-call casts. First-party components
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
        self._warn_unlifecycled_providers()
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
            if self._token is not None:  # pragma: no branch
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
            elif shared is not provider:  # pragma: no branch
                target._rebind_provider(shared)  # type: ignore[attr-defined]  # noqa: SLF001  # ty: ignore[unresolved-attribute]

    def _warn_unlifecycled_providers(self) -> None:
        """Warn when a Component holds a Provider that is not lifecycled correctly.

        Components built with `Sync(provider)` borrow the provider's client
        but do not lifecycle it. The user must list the provider in `uses=`
        *before* the Component so the provider opens first.

        Two warnings:

        1. The provider is missing from `uses=`. The provider is never opened
           or closed, so its client leaks on shutdown.
        2. The provider is in `uses=` but listed after the dependent
           Component. `Grelmicro.__aenter__` enters items in declaration
           order. Providers with lazy resources (`PostgresProvider` builds
           its pool on `__aenter__`) raise `OutOfContextError` when the
           Component opens first.
        """
        import warnings  # noqa: PLC0415

        for index, item in enumerate(self._items):
            target = getattr(item, "backend", item)
            provider = getattr(target, "_provider", None)
            if provider is None:
                continue
            owns = getattr(target, "_owns_provider", True)
            if owns:
                continue
            try:
                provider_index = self._items.index(provider)
            except ValueError:
                warnings.warn(
                    f"{type(target).__name__} holds a "
                    f"{type(provider).__name__} that is not listed in "
                    f"Grelmicro(uses=[...]). The provider will not be "
                    f"lifecycled with the app and its connection will "
                    f"leak. Add the provider to uses= so it is opened "
                    f"and closed with the components that depend on it.",
                    UserWarning,
                    stacklevel=3,
                )
                continue
            if provider_index > index:
                warnings.warn(
                    f"{type(provider).__name__} is listed after "
                    f"{type(target).__name__} in Grelmicro(uses=[...]). "
                    f"Providers must be listed before the components that "
                    f"depend on them so they open first.",
                    UserWarning,
                    stacklevel=3,
                )


def _sys_exc_info_or_none() -> tuple[Any, Any, Any]:
    """Return current exception triple (or three Nones if not in handler)."""
    import sys  # noqa: PLC0415

    return sys.exc_info()


def _maybe_wrap_first_party_backend(item: object) -> Component | None:
    """Wrap a first-party backend in the matching Component, or return None.

    Imports are lazy so unused submodules stay out of `import grelmicro`.
    The user importing `RedisCacheAdapter` already loads `grelmicro.cache`,
    so the lazy import here is a cache hit.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.cache._protocol import CacheBackend  # noqa: PLC0415
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakers,
        RateLimiters,
    )
    from grelmicro.resilience._protocol import (  # noqa: PLC0415
        CircuitBreakerBackend,
        RateLimiterBackend,
    )
    from grelmicro.sync._component import Sync  # noqa: PLC0415
    from grelmicro.sync.abc import SyncBackend  # noqa: PLC0415

    if isinstance(item, CacheBackend):
        return Cache(item)
    if isinstance(item, SyncBackend):
        return Sync(item)
    if isinstance(item, CircuitBreakerBackend):
        return CircuitBreakers(item)
    if isinstance(item, RateLimiterBackend):
        return RateLimiters(item)
    return None


class ComponentAlreadyRegisteredError(GrelmicroError, RuntimeError):
    """Raised when registering a different component under an existing `(kind, name)` key."""


class ComponentNotRegisteredError(GrelmicroError, LookupError):
    """Raised when resolving a component that has not been registered."""


class NoActiveAppError(GrelmicroError, LookupError):
    """Raised by `Grelmicro.current()` when called outside any `async with micro:` block."""
