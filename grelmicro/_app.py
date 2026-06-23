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

from grelmicro._component import Component, instantiate_if_class
from grelmicro.errors import (
    GrelmicroError,
    MultipleActiveAppsError,
    OutOfContextError,
)
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping
    from types import TracebackType

    from grelmicro.cache._component import Cache
    from grelmicro.coordination._component import Coordination
    from grelmicro.log._component import Log
    from grelmicro.trace._component import Trace
else:
    # Runtime fallback so `typing.get_type_hints(Grelmicro)` resolves the
    # `coordination` / `cache` property annotations without forcing first-party
    # submodules to load at `import grelmicro`. Real types are visible to
    # static type checkers via the `TYPE_CHECKING` branch above.
    Cache = Any
    Coordination = Any
    Log = Any
    Trace = Any

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")

_active_apps: list[Grelmicro] = []
"""Apps currently inside their `async with` block, process-wide.

Unlike `_current_micro` (per asyncio task), this is a single process-global
list so `Grelmicro.__aenter__` can refuse to open a second overlapping app
whose `Log`/`Trace` would clobber the active app's global-state snapshots.
"""


_GLOBAL_STATE_KINDS = frozenset({"log", "trace"})
"""Component kinds that own process-global state (root logger, tracer).

Two overlapping apps that each register one of these would restore the
shared global out of order, so the second is blocked. Apps without them
overlap freely, matching how web frameworks treat multiple app objects.
"""

_active_bulkhead: ContextVar[Mapping[tuple[str, str], Component]] = ContextVar(
    "grelmicro_active_bulkhead"
)
"""Component overrides installed by the active `Bulkhead` scope, keyed by `(kind, name)`.

`Grelmicro.get` consults this before its own registry so a Pattern resolving
its default backend inside the scope picks up the bulkhead's `uses=`
component. A Pattern with an explicit `backend=` never calls `get`, so
explicit choices always win.
"""


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every item attached to the
    app (components, task managers, health registries, custom async context
    managers, ...) and opens them as a single async context manager. Two
    `Grelmicro` instances in the same process are fully independent.

    The conventional variable name is `micro`:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.coordination import Coordination
    from grelmicro.task import Tasks

    tasks = Tasks()

    micro = Grelmicro(uses=[
        Coordination(lock=RedisLockAdapter()),
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
            Iterable[
                AbstractAsyncContextManager[object]
                | type[AbstractAsyncContextManager[object]]
            ]
            | None,
            Doc(
                """
                Items registered at construction time. Equivalent to a
                sequence of `.use(item)` calls in the same order. Accepts
                `Component` instances (registered with `(kind, name)`
                lookup, exposed on `micro.<kind>`), bare adapter or component
                classes (instantiated for you), `Provider` instances (a lone
                Provider registers a default Component per kind it serves), and
                plain async context managers (lifecycled only, caller holds the
                reference). Two bare Providers with no Components raise
                `AmbiguousProviderError`.
                """,
            ),
        ] = None,
        strict: Annotated[
            bool,
            Doc(
                """
                Raise `LifecycleOrderError` instead of warning when a
                Component holds a Provider that is missing from `uses=`
                or listed after the dependent Component. Default `False`
                preserves the lenient warn-only behavior so existing
                apps keep starting.
                """,
            ),
        ] = False,
        allow_multiple: Annotated[
            bool,
            Doc(
                """
                Allow this app to run while another `Grelmicro` app is
                active in the same process. Off by default: components like
                `Log` and `Trace` own process-global state that two
                overlapping app lifecycles would restore out of order.
                Setting `True` opts out of the guard when you are sure no
                two active apps configure the same global state.
                """,
            ),
        ] = False,
    ) -> None:
        """Initialize the app and register any items passed at construction."""
        self._items: list[AbstractAsyncContextManager[object]] = []
        self._by_key: dict[tuple[str, str], Component] = {}
        self._by_kind: dict[str, Component] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._token: Any = None
        self._strict = strict
        self._allow_multiple = allow_multiple
        self._pending_providers: list[Provider] = []
        self._deferring_provider_defaults = uses is not None
        if uses is not None:
            try:
                for item in uses:
                    self.use(item)
            finally:
                self._deferring_provider_defaults = False
            self._register_provider_defaults()

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
            AbstractAsyncContextManager[object]
            | type[AbstractAsyncContextManager[object]],
            Doc(
                """
                The item to register and lifecycle with the app. A `Component`
                instance is indexed under `(kind, name)` and exposed on
                `micro.<kind>`. A first-party backend is auto-wrapped into its
                matching `Component`. A `Provider` on an app with no Components
                registers one default Component per kind it serves. A zero-arg
                class is instantiated first. Any other async context manager is
                just lifecycled, and the caller keeps the reference.
                """,
            ),
        ],
    ) -> None:
        """Register an item to be lifecycled with the app.

        Four shapes are accepted:

        1. A `Component` instance: registered with `(kind, name)` lookup and
           exposed on `micro.<kind>`.
        2. A first-party backend (e.g. `RedisLockAdapter`): auto-wrapped
           into the matching `Component` (`Coordination` for lock backends,
           `Cache` for cache backends) before registration.
        3. A `Provider` (e.g. `RedisProvider`): always lifecycled. On an app
           with no Components, it also registers one default Component per kind
           it serves. Once any Component is registered, the Provider is
           lifecycle-only.
        4. Any other async context manager: just lifecycled with the app,
           the caller keeps the reference.

        A bare class (no parens) is instantiated first, in the spirit of
        FastAPI's `Depends(dep)`, so `use(MemoryLockAdapter)` matches
        `use(MemoryLockAdapter())`.

        ```python
        # Auto-wrapped first-party backend
        micro.use(RedisLockAdapter())          # registered as (coordination, default)
        micro.use(RedisCacheAdapter())         # registered as (cache, default)

        # Provider on an empty app: default Component per served kind
        micro.use(RedisProvider("redis://localhost"))

        # Explicit Component when a non-default name is needed
        micro.use(Coordination(lock=RedisLockAdapter(), name="analytics"))

        # Plain async context manager: lifecycled only, caller holds reference
        tasks = Tasks()
        micro.use(tasks)
        ```

        Returns `None`. Mirrors FastAPI's `app.include_router(router)`
        pattern: pure side-effect registration. To access registered
        components, use the typed `micro.coordination` / `micro.cache`
        properties or `micro.get(kind, name)`. For plain async context
        managers, the caller already holds the reference.

        Raises:
            ComponentAlreadyRegisteredError: A different component is already
                registered under the same `(kind, name)` key. Plain async
                context managers do not raise; they are appended.
        """
        # A bare class (no parens) is instantiated with no arguments, in the
        # spirit of FastAPI's `Depends(dep)`: pass the reference, the framework
        # calls it. Useful for zero-arg adapters like `MemoryLockAdapter`.
        item = instantiate_if_class(item)
        if isinstance(item, Provider):
            self._use_provider(item)
            return
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
        self._register_component(component)

    def _use_provider(self, provider: Provider) -> None:
        """Lifecycle a bare Provider and queue its default-Component registration.

        The Provider is always lifecycled here. When the app holds no explicit
        Component, it also auto-registers one default Component per kind it
        serves. Inside `Grelmicro(uses=[...])` that registration is deferred to
        `_register_provider_defaults` so an explicit Component listed anywhere in
        the list wins. A standalone `use(provider)` call registers right away,
        against whatever is registered so far.
        """
        self._items.append(provider)
        if self._deferring_provider_defaults:
            self._pending_providers.append(provider)
        elif not self._by_key:
            self._register_one_provider(provider)

    def _register_component(self, component: Component) -> None:
        """Index a Component under `(kind, name)` and append it for lifecycle.

        Raises:
            ComponentAlreadyRegisteredError: A different component already holds
                the same `(kind, name)` key, or a same-kind singleton is already
                registered.
        """
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
        if getattr(component, "singleton", False):
            for other in self._by_key.values():
                if other.kind == component.kind:
                    msg = (
                        f"component kind {component.kind!r} is a singleton "
                        f"and is already registered as {other.name!r}. It "
                        f"configures process-global state, so only one may "
                        f"exist per Grelmicro app."
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

    def _register_provider_defaults(self) -> None:
        """Auto-register default Components from Providers passed bare to `uses=`.

        Runs once after every item in `uses=` is processed. When the app holds
        no explicit Component, each kind a listed Provider serves (`coordination`,
        `cache`, `ratelimiter`, `circuitbreaker`) gets one default-named
        Component wired to that Provider. Listing any explicit Component turns
        this off entirely, so the `uses=` list reads the same way it runs: a
        lone Provider is a full default app, a Provider mixed with Components is
        lifecycle-only and the Components own the wiring.

        Raises:
            AmbiguousProviderError: Two or more bare Providers are listed with no
                explicit Component, so the default for a shared kind is
                ambiguous.
        """
        pending = self._pending_providers
        self._pending_providers = []
        if not pending or self._by_key:
            return
        if len(pending) > 1:
            names = ", ".join(type(p).__name__ for p in pending)
            msg = (
                f"Grelmicro(uses=[...]) lists multiple providers ({names}) "
                f"with no components, so the default component for each kind is "
                f"ambiguous. Wrap each provider in the components it should "
                f"serve, for example Cache(provider) or "
                f"RateLimiterRegistry(provider)."
            )
            raise AmbiguousProviderError(msg)
        self._register_one_provider(pending[0])

    def _register_one_provider(self, provider: Provider) -> None:
        """Register one default Component per kind `provider` serves.

        A kind is served when the matching Component builds without the provider
        raising `NotImplementedError`. The provider stays lifecycled where it was
        listed, the Components borrow its client and are not lifecycled again.
        """
        for component in _default_components_for_provider(provider):
            self._register_component(component)

    def get(
        self,
        kind: Annotated[
            str,
            Doc(
                """
                Component category, matching the `kind` class attribute on
                the registered component (`"coordination"`, `"cache"`, `"ratelimiter"`,
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
                `Coordination(lock=backend, name="analytics")`.
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
        overrides = _active_bulkhead.get(None)
        if overrides is not None:
            override = overrides.get((kind, name))
            if override is not None:
                return override
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
            async with micro.override(Coordination(lock=MockLock())):
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
    def providers(self) -> tuple[Provider, ...]:
        """Active `Provider` instances, in registration order, deduped by identity.

        Includes Providers listed in `uses=` and those adopted from
        Components that borrow them (see `_discover_shared_providers`). One
        Provider feeding several Components appears once. Useful for
        introspection and for `HealthChecks(auto_health=True)`, which
        registers a `provider:{short_name}` check per entry.
        """
        seen: dict[int, Provider] = {}
        for item in self._items:
            if isinstance(item, Provider) and id(item) not in seen:
                seen[id(item)] = item
        return tuple(seen.values())

    @property
    def coordination(self) -> Coordination:
        """The registered `Coordination` component.

        Resolves the default-named entry, or the sole entry of kind
        `coordination`.
        """
        return self._resolve_kind("coordination")

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

    def _bind_current(self) -> Any:  # noqa: ANN401
        """Set this app as `current()` for the running task, return a reset token.

        Used by an integration (the FastAPI middleware) to propagate the
        active app into a request task that runs outside the
        `async with micro:` block. Pair every call with `_reset_current`.
        """
        return _current_micro.set(self)

    @staticmethod
    def _reset_current(token: Any) -> None:  # noqa: ANN401
        """Reset the `current()` binding from a `_bind_current` token."""
        _current_micro.reset(token)

    def _owns_global_state(self) -> bool:
        """Return True if any registered item configures process-global state."""
        return any(
            getattr(item, "kind", None) in _GLOBAL_STATE_KINDS
            for item in self._items
        )

    async def __aenter__(self) -> Self:
        """Open every registered item in registration order.

        The active-app `ContextVar` is set before entries so items can call
        `Grelmicro.current()` from their `__aenter__`. On partial-startup
        failure, the token is reset before unwinding.
        """
        if self._exit_stack is not None:
            raise OutOfContextError(self, "__aenter__")
        if (
            not self._allow_multiple
            and self._owns_global_state()
            and any(app._owns_global_state() for app in _active_apps)  # noqa: SLF001
        ):
            raise MultipleActiveAppsError
        self._discover_shared_providers()
        self._warn_unlifecycled_providers()
        self._resolve_provider_sharing()
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        self._token = _current_micro.set(self)
        _active_apps.append(self)
        try:
            for item in self._items:
                await self._exit_stack.enter_async_context(item)
        except BaseException:
            _active_apps.remove(self)
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
            if self in _active_apps:  # pragma: no branch
                _active_apps.remove(self)
            self._exit_stack = None

    def _discover_shared_providers(self) -> None:
        """Adopt Providers reachable from components but absent from `uses=`.

        A Component built as `Coordination(provider)` borrows the provider's
        client but does not own its lifecycle. When that provider is not listed
        in `uses=`, discover it here and lifecycle it once, inserted just before
        the first Component that holds it. One Provider feeds many Components
        without the user repeating it in `uses=`:

        ```python
        micro = Grelmicro(uses=[Coordination(redis), Cache(redis)])  # adopted
        ```

        A `Coordination` holds two backends (a lock backend and an election
        backend), each able to borrow its own Provider, so both are walked and
        each borrowed Provider is adopted.

        Providers the user already listed are left untouched, so their declared
        order still applies and the ordering check in
        `_warn_unlifecycled_providers` still fires on a late listing. Adapters
        that own their provider (built from env, no user instance) are handled
        by `_resolve_provider_sharing`.
        """
        listed = {id(item) for item in self._items}
        discovered: dict[int, AbstractAsyncContextManager[object]] = {}
        rebuilt: list[AbstractAsyncContextManager[object]] = []
        for item in self._items:
            for target in _iter_provider_backends(item):
                provider = getattr(target, "_provider", None)
                owns = getattr(target, "_owns_provider", True)
                if (
                    provider is not None
                    and not owns
                    and id(provider) not in listed
                    and id(provider) not in discovered
                ):
                    discovered[id(provider)] = provider
                    rebuilt.append(provider)
            rebuilt.append(item)
        self._items = rebuilt

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
            for target in _iter_provider_backends(item):
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
        """Warn when a user-listed Provider is ordered after its Component.

        A Component built with `Coordination(provider)` borrows the provider's
        client but does not lifecycle it. `Grelmicro.__aenter__` enters items in
        declaration order, so a Provider listed *after* the Component that
        depends on it opens too late. Providers with lazy resources
        (`PostgresProvider` builds its pool on `__aenter__`) then raise
        `OutOfContextError` when the Component opens first.

        Providers absent from `uses=` are adopted by
        `_discover_shared_providers` and inserted ahead of their Component, so
        only the explicit-but-misordered listing reaches this check.

        Reported as `UserWarning` by default, or as `LifecycleOrderError`
        when the app was built with `strict=True`.
        """
        for index, item in enumerate(self._items):
            for target in _iter_provider_backends(item):
                provider = getattr(target, "_provider", None)
                if provider is None:
                    continue
                owns = getattr(target, "_owns_provider", True)
                if owns:
                    continue
                self._report_provider_lifecycle(target, provider, index)

    def _report_provider_lifecycle(
        self,
        target: object,
        provider: object,
        index: int,
    ) -> None:
        """Warn or raise when a user-listed Provider is ordered after its Component.

        Discovery adopts Providers missing from `uses=` and inserts them ahead
        of their Component, so the Provider is always present in `self._items`
        and only the explicit-but-misordered listing reaches this check.
        """
        import warnings  # noqa: PLC0415

        if self._items.index(provider) > index:  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            msg = (
                f"{type(provider).__name__} is listed after "
                f"{type(target).__name__} in Grelmicro(uses=[...]). "
                f"Providers must be listed before the components that "
                f"depend on them so they open first."
            )
            if self._strict:
                raise LifecycleOrderError(msg)
            warnings.warn(msg, UserWarning, stacklevel=3)


def _iter_provider_backends(item: object) -> list[object]:
    """Return the provider-holding backends to inspect for `item`.

    Most components expose one backend via `backend`. A `Coordination`
    component holds a lock backend and an election backend, either of which
    may own a Provider, so both are returned. A plain item with no backend
    is inspected directly.
    """
    lock_backend = getattr(item, "_lock_backend", None)
    election_backend = getattr(item, "_election_backend", None)
    if lock_backend is not None or election_backend is not None:
        return [b for b in (lock_backend, election_backend) if b is not None]
    return [getattr(item, "backend", item)]


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
    from grelmicro.coordination._component import (  # noqa: PLC0415
        Coordination,
    )
    from grelmicro.coordination.abc import (  # noqa: PLC0415
        LeaderElectionBackend,
        LockBackend,
    )
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerRegistry,
        RateLimiterRegistry,
    )
    from grelmicro.resilience._protocol import (  # noqa: PLC0415
        CircuitBreakerBackend,
        RateLimiterBackend,
    )

    if isinstance(item, CacheBackend):
        return Cache(item)
    if isinstance(item, LeaderElectionBackend):
        return Coordination(election=item)
    if isinstance(item, LockBackend):
        return Coordination(lock=item)
    if isinstance(item, CircuitBreakerBackend):
        return CircuitBreakerRegistry(item)
    if isinstance(item, RateLimiterBackend):
        return RateLimiterRegistry(item)
    return None


def _default_components_for_provider(provider: Provider) -> list[Component]:
    """Build one default Component per kind `provider` serves.

    Walks the provider factories. A factory that raises `NotImplementedError`
    means the provider does not serve that kind, so the matching Component is
    skipped. `coordination` wires whichever of the lock, election, and schedule
    backends the provider ships. Imports are lazy so a provider that serves only
    one kind never loads the other component modules.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.coordination._component import (  # noqa: PLC0415
        Coordination,
    )
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerRegistry,
        RateLimiterRegistry,
    )

    components: list[Component] = []

    lock = _provider_backend_or_none(provider.lock)
    election = _provider_backend_or_none(provider.leaderelection)
    schedule = _provider_backend_or_none(provider.schedule)
    if lock is not None or election is not None or schedule is not None:
        components.append(
            Coordination(lock=lock, election=election, schedule=schedule)
        )

    cache = _provider_backend_or_none(provider.cache)
    if cache is not None:
        components.append(Cache(cache))

    ratelimiter = _provider_backend_or_none(provider.ratelimiter)
    if ratelimiter is not None:
        components.append(RateLimiterRegistry(ratelimiter))

    circuitbreaker = _provider_backend_or_none(provider.circuitbreaker)
    if circuitbreaker is not None:
        components.append(CircuitBreakerRegistry(circuitbreaker))

    return components


def _provider_backend_or_none(factory: Any) -> Any:  # noqa: ANN401
    """Call a provider factory, returning `None` when the kind is unsupported."""
    try:
        return factory()
    except NotImplementedError:
        return None


class ComponentAlreadyRegisteredError(GrelmicroError, RuntimeError):
    """Raised when registering a different component under an existing `(kind, name)` key."""


class ComponentNotRegisteredError(GrelmicroError, LookupError):
    """Raised when resolving a component that has not been registered."""


class NoActiveAppError(GrelmicroError, LookupError):
    """Raised by `Grelmicro.current()` when called outside any `async with micro:` block."""


class LifecycleOrderError(GrelmicroError, ValueError):
    """Raised when `Grelmicro(strict=True)` detects misordered provider/component lifecycles."""


class AmbiguousProviderError(GrelmicroError, ValueError):
    """Raised when two bare Providers in `uses=` make the default component ambiguous."""
