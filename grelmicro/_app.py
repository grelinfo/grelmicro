"""The Grelmicro app object."""

from __future__ import annotations

from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from contextvars import ContextVar
from dataclasses import replace
from functools import partial
from threading import Lock as ThreadLock
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Protocol,
    Self,
    cast,
    overload,
)

from typing_extensions import Doc

from grelmicro._component import Component, Usable, instantiate_if_class
from grelmicro._diagnostics import (
    AMBIENT_BINDING,
    PROVIDER_ORDER,
    diagnostic,
)
from grelmicro._discovery import integration_names, load_integration
from grelmicro._environment import (
    report_unmet_requirements,
    resolve_environment,
    strict_message,
    unmet_requirements,
)
from grelmicro.errors import (
    AmbientBindingWarning,
    BackendScopeError,
    GrelmicroError,
    MultipleActiveAppsError,
    OutOfContextError,
)
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Mapping
    from types import TracebackType

    from grelmicro._describe import AppReport, CheckReport
    from grelmicro.cache._component import Cache
    from grelmicro.coordination._component import Coordination
    from grelmicro.health._checks import HealthChecks
    from grelmicro.log._component import Log
    from grelmicro.metrics._component import Metrics
    from grelmicro.outbox._component import Outbox
    from grelmicro.resilience._components import (
        CircuitBreakerComponent,
        RateLimiterComponent,
    )
    from grelmicro.trace._component import Trace
    from grelmicro.types import Environment
else:
    # Runtime fallback so `typing.get_type_hints(Grelmicro)` resolves the
    # `coordination` / `cache` property annotations without forcing first-party
    # submodules to load at `import grelmicro`. Real types are visible to
    # static type checkers via the `TYPE_CHECKING` branch above.
    AppReport = Any
    Cache = Any
    CheckReport = Any
    CircuitBreakerComponent = Any
    Coordination = Any
    Environment = Any
    HealthChecks = Any
    Log = Any
    Metrics = Any
    Outbox = Any
    RateLimiterComponent = Any
    Trace = Any

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")

_active_apps: list[Grelmicro] = []
"""Apps currently inside their `async with` block, process-wide.

Unlike `_current_micro` (per asyncio task), this is a single process-global
list so `Grelmicro.__aenter__` can refuse to open a second overlapping app
whose `Log`/`Trace` would clobber the active app's global-state snapshots.
"""

_active_apps_lock = ThreadLock()
"""Serializes the process-global active app guard and reservation."""


_GLOBAL_STATE_KINDS = frozenset({"log", "trace", "metrics"})
"""Component kinds that own process-global state (root logger, tracer, meter).

Two overlapping apps that each register one of these would restore the
shared global out of order, so the second is blocked. Apps without them
overlap freely, matching how web frameworks treat multiple app objects.
"""


def _item_owns_global_state(item: object) -> bool:
    """Return True if a registered item configures process-global state.

    A kind in `_GLOBAL_STATE_KINDS` owns global state by default. An item may
    refine this with an `owns_global_state()` method: an auto-disabled `Trace`
    (default exporter, no endpoint) installs nothing, so it opts out and lets
    overlapping apps carry it.
    """
    if getattr(item, "kind", None) not in _GLOBAL_STATE_KINDS:
        return False
    refine = getattr(item, "owns_global_state", None)
    return bool(refine()) if callable(refine) else True


_AMBIENT_KINDS = frozenset(
    {"coordination", "cache", "ratelimiter", "circuitbreaker"}
)
"""Component kinds whose patterns resolve their backend through `current()`.

A `Lock`, `@cached`, `RateLimiter`, or `CircuitBreaker` that omits `backend=`
looks up the active app per call. Inside a request handler that only works
when `GrelmicroMiddleware` binds the app per request (see
`Grelmicro.check_ambient_binding`).
"""

_active_bulkhead: ContextVar[Mapping[tuple[str, str], Component]] = ContextVar(
    "grelmicro_active_bulkhead"
)
"""Component overrides installed by the active `Bulkhead` scope, keyed by `(kind, name)`.

`Grelmicro.get` consults this before its own registrations so a Pattern resolving
its default backend inside the scope picks up the bulkhead's `uses=`
component. A Pattern with an explicit `backend=` never calls `get`, so
explicit choices always win.
"""


class Grelmicro:
    """The grelmicro application container.

    A `Grelmicro` is the user-owned root that holds every item attached to the
    app (components, task managers, custom async context
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

    @tasks.every(seconds=5)
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
            Iterable[Usable | None] | None,
            Doc(
                """
                Items registered at construction time, in the given order.
                Accepts `Component` instances (registered with `(kind, name)`
                lookup, exposed on `micro.<kind>`), bare adapter or component
                classes (instantiated for you), `Provider` instances (a lone
                Provider registers a default Component per kind it serves,
                `outbox` aside), and
                plain async context managers (lifecycled only, caller holds the
                reference). Two bare Providers with no Components raise
                `AmbiguousProviderError`.

                A `None` entry is skipped, so a component registered only for
                one backend stays a plain expression:
                `uses=[Log(), redis if backend == "redis" else None]`.

                Annotate a list you build beforehand with `Usable`.
                """,
            ),
        ] = None,
        environment: Annotated[
            Environment | None,
            Doc(
                """
                The deployment tier this app runs in: `"development"`,
                `"test"`, `"staging"` or `"production"`. Falls back to
                `GREL_ENVIRONMENT`, which is read whatever `GREL_ENV_LOAD`
                says, and stays `None` when neither declares one.

                `"staging"` and `"production"` make an unmet backend scope a
                `BackendScopeError` at startup, `"development"` and `"test"`
                silence it, and an undeclared tier reports it once as a
                warning. The declared value also becomes the OpenTelemetry
                `deployment.environment.name` resource attribute. See
                [the backend check](deployment.md#the-backend-check).
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
        self._environment = resolve_environment(environment)
        self._allow_multiple = allow_multiple
        self._pending_providers: list[Provider] = []
        self._deferring_provider_defaults = uses is not None
        if uses is not None:
            try:
                for item in uses:
                    # A `None` entry is a conditional registration that did
                    # not apply.
                    if item is not None:
                        self.use(item)
            finally:
                self._deferring_provider_defaults = False
            self._register_provider_defaults()

    @property
    def environment(self) -> Environment | None:
        """The declared deployment tier, or `None` when nothing declares one.

        Resolved once at construction from `Grelmicro(environment=...)`, then
        `GREL_ENVIRONMENT`. A value naming no tier reads as `None`.
        """
        return self._environment

    def check_backends(
        self,
        environment: Annotated[
            Environment,
            Doc(
                """
                The tier to answer for. Defaults to `"production"`, so the
                answer holds for the deployment rather than for the process
                running the check.
                """,
            ),
        ] = "production",
    ) -> None:
        """Check every bound backend against what its component requires.

        Asks the question the deployed app will ask, from a process that
        declares something else, so a test catches the wiring before a pod
        does:

        ```python
        def test_backends_hold_across_replicas() -> None:
            micro.check_backends()
        ```

        Raises:
            BackendScopeError: If any bound backend reaches less far than its
                component requires, naming every one of them.
        """
        unmet = unmet_requirements(self._items)
        if unmet:
            raise BackendScopeError(strict_message(unmet, environment))

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
            Usable,
            Doc(
                """
                The item to register and lifecycle with the app. A `Component`
                instance is indexed under `(kind, name)` and exposed on
                `micro.<kind>`. A first-party backend is auto-wrapped into its
                matching `Component`. A `Provider` on an app with no Components
                registers one default Component per kind it serves, `outbox`
                aside. A zero-arg class is instantiated first. Any other async context manager is
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
           with no Components, it also registers one default Component per
           kind it serves, `outbox` aside. Once any Component is registered,
           the Provider is lifecycle-only.
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
                context managers do not raise. They are appended.
            TypeError: If `item` is `None`. `Grelmicro(uses=[...])` skips a
                `None` entry, a single call does not.
        """
        if item is None:
            msg = (
                "use(None) registers nothing. Guard the call with `if`, or "
                "move the conditional into Grelmicro(uses=[...]), which "
                "skips None entries."
            )
            raise TypeError(msg)
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

        The Provider is always lifecycled here. It also auto-registers one
        default Component for every kind it serves that no explicit Component
        already claims. Inside `Grelmicro(uses=[...])` that registration is
        deferred to `_register_provider_defaults` so a Component listed anywhere
        in the list wins, whatever the order. A standalone `use(provider)` call
        registers right away, against whatever is claimed so far.
        """
        self._items.append(provider)
        if self._deferring_provider_defaults:
            self._pending_providers.append(provider)
        else:
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
        # Either side declaring the kind a singleton is enough. Checking only
        # the incoming component let a plain component of the same kind
        # register after a singleton, which is the case the guard exists for.
        # Read with `getattr` rather than as a protocol member: `Component` is
        # runtime-checkable, so a declared attribute would make every
        # third-party component that omits it fail `isinstance` and silently
        # fall through to the plain context-manager path.
        for other in self._by_key.values():
            if other.kind == component.kind and (
                getattr(component, "singleton", False)
                or getattr(other, "singleton", False)
            ):
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

        Runs once after every item in `uses=` is processed. Each kind a listed
        Provider serves (`coordination`, `cache`, `ratelimiter`,
        `circuitbreaker`) gets one default-named Component wired to that
        Provider, unless an explicit Component already claims that kind.
        Explicit wins, the Provider fills the rest.

        Back-off is per kind, so a Component of an unrelated kind
        (`HealthChecks`, `Log`, `Trace`) leaves the provider defaults alone.

        Two or more Providers never fill defaults, because neither can be the
        default for a kind they both serve.

        Raises:
            AmbiguousProviderError: Two or more bare Providers are listed with
                no explicit Component, so the default for each kind is
                ambiguous.
        """
        pending = self._pending_providers
        self._pending_providers = []
        if not pending:
            return
        if len(pending) > 1:
            # Two Providers cannot both be the default for a shared kind, so
            # neither fills anything. With Components present the app is
            # wiring explicitly and they are lifecycle-only, without them
            # there is no way to guess and it is worth saying so early.
            if self._by_key:
                return
            names = ", ".join(type(p).__name__ for p in pending)
            msg = (
                f"Grelmicro(uses=[...]) lists multiple providers ({names}) "
                f"with no components, so the default component for each kind is "
                f"ambiguous. Wrap each provider in the components it should "
                f"serve, for example Cache(provider) or "
                f"RateLimiterComponent(provider)."
            )
            raise AmbiguousProviderError(msg)
        self._register_one_provider(pending[0])

    def _register_one_provider(self, provider: Provider) -> None:
        """Register a default Component for every unclaimed kind `provider` serves.

        A kind is served when the matching Component builds without the provider
        raising `NotImplementedError`. A kind an explicit Component already holds
        is skipped, so an explicit choice is never overwritten. The provider
        stays lifecycled where it was listed, the Components borrow its client
        and are not lifecycled again.
        """
        claimed = {component.kind for component in self._by_key.values()}
        for component in _default_components_for_provider(provider):
            if component.kind in claimed:
                continue
            self._register_component(component)

    @overload
    def get[ComponentT: Component](
        self,
        kind: type[ComponentT],
        name: str = "default",
    ) -> ComponentT: ...

    @overload
    def get(self, kind: str, name: str = "default") -> Any: ...  # noqa: ANN401

    def get(
        self,
        kind: Annotated[
            str | type[Component],
            Doc(
                """
                The component class to resolve, such as `Cache` or
                `RateLimiterComponent`, which keeps the return type. Or the
                `kind` string on the registered component (`"coordination"`,
                `"cache"`, `"ratelimiter"`, `"circuitbreaker"`, `"log"`,
                `"trace"`, `"metrics"`, `"health"`), which returns `Any` and
                also resolves third-party kinds.
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
    ) -> Any:
        """Resolve a registered component by `(kind, name)`.

        Pass the class to keep the type through resolution:

        ```python
        cache = micro.get(Cache)                          # -> Cache
        limiter = micro.get(RateLimiterComponent, "api")  # -> RateLimiterComponent
        ```

        Pass the kind string for a component grelmicro does not define, such
        as one a third-party package registers. That form returns `Any`,
        because the registration is dynamic and cannot be typed without a
        global registry:

        ```python
        mailer = micro.get("mailer")                      # -> Any
        ```

        Raises:
            ComponentNotRegisteredError: If no component matches.
        """
        if isinstance(kind, type):
            kind = kind.kind
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
    async def fake(self) -> AsyncIterator[None]:
        """Swap every backed component onto an in-process store for a block.

        Each registered `Coordination`, `Cache`, `RateLimiterComponent`, and
        `CircuitBreakerComponent` is replaced by one wired to a fresh
        `MemoryProvider`, under the same name. Everything is restored on exit.
        A test then runs the real code paths against real primitives, with no
        Redis and no Postgres:

        ```python
        async with micro:
            async with micro.fake():
                await checkout("cart-1")
        ```

        Components with no backend to fake (`Log`, `Trace`, `Metrics`,
        `HealthChecks`) are left alone, and so is `Outbox`, which carries
        handlers and a relay that a swap would drop. Override those by hand
        with `micro.override(...)` when a test needs them.

        Raises:
            OutOfContextError: If called outside an open `async with micro:`
                block, which is what `override` scopes to.
        """
        if self._exit_stack is None:
            raise OutOfContextError(self, "fake")
        from grelmicro.providers.memory import MemoryProvider  # noqa: PLC0415

        provider = MemoryProvider()
        replacements = _fake_components(self.components, provider)
        async with provider, self.override(*replacements):
            yield

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
                exit, even if the block raises and even if one of these
                components fails to open.
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
            # The index is mutated one component at a time, so the restore
            # has to cover the loop as well as the block. A component that
            # fails to open leaves the ones before it already installed.
            try:
                for component in components:
                    key = (component.kind, component.name)
                    self._by_key[key] = component
                    if component not in self._items:  # pragma: no branch
                        self._items.append(component)
                    if component.name == "default":  # pragma: no branch
                        self._by_kind[component.kind] = component
                    # `Component` is an async context manager; ty misreads the
                    # protocol's `Self`-returning `__aenter__` as incompatible
                    # with its own AbstractAsyncContextManager base.
                    await stack.enter_async_context(component)  # ty: ignore[invalid-argument-type]
                yield
            finally:
                self._by_key = snapshot_by_key
                self._items = snapshot_items
                self._by_kind = snapshot_by_kind

    def describe(
        self,
        app: Annotated[  # noqa: ANN401
            Any,
            Doc(
                """
                The Starlette, FastAPI, or FastStream application this app is
                installed on. Pass it to include the ambient binding check,
                which catches a forgotten `micro.install(app)`. Omit it for a
                service with no web framework.
                """,
            ),
        ] = None,
    ) -> AppReport:
        """Return a structured report of what this app is wired with.

        Answers what got wired, from what, reachable how far, and configured
        with what. Credential-like values are masked.

        ```python
        report = micro.describe()

        assert report.ok
        assert [c.kind for c in report.components] == ["cache", "coordination"]
        ```

        `python -m grelmicro check` renders the same report and turns its
        checks into an exit code. Read more in the [wiring](wiring.md) docs.
        """
        from grelmicro._describe import build_report  # noqa: PLC0415

        report = build_report(self)
        if app is None:
            return report
        return replace(
            report, checks=(*report.checks, self._ambient_check(app))
        )

    def _ambient_check(self, app: object) -> CheckReport:
        """Return the ambient binding check for `app`.

        A framework this app does not recognize is reported rather than
        raised, because `describe` answers questions and never blocks.
        """
        from grelmicro._describe import CheckReport  # noqa: PLC0415

        try:
            bound = self.check_ambient_binding(app)
        except TypeError as exc:
            return CheckReport(
                name="ambient-binding", status="warn", detail=str(exc)
            )
        if bound:
            return CheckReport(
                name="ambient-binding",
                status="ok",
                detail="patterns resolve their backend inside request handlers",
            )
        return CheckReport(
            name="ambient-binding",
            status="fail",
            detail=(
                "ambient components are registered but the binding middleware "
                "is missing, so a pattern that omits backend= raises "
                "OutOfContextError on every request. Call micro.install(app)"
            ),
        )

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

    @property
    def metrics(self) -> Metrics:
        """The registered `Metrics` component (default-named, or sole entry of kind `metrics`)."""
        return self._resolve_kind("metrics")

    @property
    def health(self) -> HealthChecks:
        """The registered `HealthChecks` component.

        Resolves the default-named entry, or the sole entry of kind `health`.
        """
        return self._resolve_kind("health")

    @property
    def outbox(self) -> Outbox:
        """The registered `Outbox` component (default-named, or sole entry of kind `outbox`)."""
        return self._resolve_kind("outbox")

    @property
    def ratelimiter(self) -> RateLimiterComponent:
        """The registered `RateLimiterComponent`.

        Resolves the default-named entry, or the sole entry of kind
        `ratelimiter`.
        """
        return self._resolve_kind("ratelimiter")

    @property
    def circuitbreaker(self) -> CircuitBreakerComponent:
        """The registered `CircuitBreakerComponent`.

        Resolves the default-named entry, or the sole entry of kind
        `circuitbreaker`.
        """
        return self._resolve_kind("circuitbreaker")

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

    def install(
        self,
        app: Annotated[  # noqa: ANN401
            Any,
            Doc(
                """
                A Starlette, FastAPI, or FastStream application. The framework
                is detected from the object's shape, so the same call wires any
                of them.
                """,
            ),
        ],
        *,
        ambient: Annotated[
            bool,
            Doc(
                """
                Wire per-handler ambient binding so `Lock(...)`, `@cached`,
                `RateLimiter...`, and the other patterns resolve through
                `Grelmicro.current()` inside request handlers and message
                subscribers. Default `True`. Pass `False` when handlers always
                pass an explicit `backend=` and the binding is not needed.
                """,
            ),
        ] = True,
    ) -> None:
        """Wire the app lifecycle and ambient binding in one call.

        Opens `async with micro:` alongside the framework's own lifecycle, so
        components are registered before any request or message is handled and
        closed on shutdown. A custom lifespan already passed to the framework
        keeps running, chained around this one.

        When `ambient` is `True` (the default), each request handler or message
        subscriber runs with this app bound as `Grelmicro.current()`, so
        patterns that omit `backend=` resolve ambiently.

        ```python
        from fastapi import FastAPI

        from grelmicro import Grelmicro

        micro = Grelmicro(uses=[...])
        app = FastAPI()
        micro.install(app)
        ```

        Raises:
            TypeError: If `app` is not a recognized Starlette, FastAPI, or
                FastStream application.
        """
        integration = load_integration(app)
        if integration is None:
            raise TypeError(
                _unsupported_framework_message("micro.install", app)
            )
        integration.install(app, self, ambient=ambient)

    def _ambient_component_labels(self) -> list[str]:
        """Return sorted `kind:name` labels of registered ambient components."""
        return sorted(
            f"{component.kind}:{component.name}"
            for component in self._by_key.values()
            if component.kind in _AMBIENT_KINDS
        )

    def _on_ambient_disabled(self) -> None:
        """Warn or raise when `install(ambient=False)` leaves patterns unbound.

        Called by an integration's `install` when the per-request binding is
        skipped. With ambient-resolving components registered, patterns that
        omit `backend=` would raise `OutOfContextError` on every request.
        Reported as a `UserWarning`, or as `AmbientBindingError` when the app
        was built with `strict=True`, matching the warn-or-raise policy of the
        provider-lifecycle check.
        """
        labels = self._ambient_component_labels()
        if not labels:
            return
        import warnings  # noqa: PLC0415

        msg = (
            f"install(app, ambient=False) skips the per-request binding, but "
            f"these components resolve their backend ambiently: "
            f"{', '.join(labels)}. A pattern that omits backend= will raise "
            f"OutOfContextError on every request. Keep ambient=True (the "
            f"default) or pass backend= explicitly at each call site."
        )
        msg = diagnostic(AMBIENT_BINDING, msg)
        if self._strict:
            raise AmbientBindingError(msg)
        warnings.warn(msg, AmbientBindingWarning, stacklevel=3)

    def check_ambient_binding(
        self,
        app: Annotated[  # noqa: ANN401
            Any,
            Doc("A Starlette, FastAPI, or FastStream application."),
        ],
    ) -> bool:
        """Return whether ambient pattern resolution is wired for `app`.

        Returns `True` when this app registers no ambient-resolving components
        (nothing needs binding) or when the binding middleware is installed on
        `app` by `micro.install(app)`. Returns `False` when ambient components
        are registered but the middleware is missing, so a `Lock(...)`,
        `@cached`, `RateLimiter...`, or `CircuitBreaker(...)` that omits
        `backend=` would raise `OutOfContextError` on every request.

        Call it in a test to catch a forgotten `micro.install(app)` before it
        reaches production, where the failure only surfaces on the first
        affected request:

        ```python
        def test_ambient_binding_is_wired() -> None:
            assert micro.check_ambient_binding(app)
        ```

        Raises:
            TypeError: If `app` is not a recognized Starlette, FastAPI, or
                FastStream application and ambient components are registered.
        """
        if not self._ambient_component_labels():
            return True
        integration = load_integration(app)
        if integration is None:
            raise TypeError(
                _unsupported_framework_message("check_ambient_binding", app)
            )
        return bool(integration.is_bound(app))

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
        return any(_item_owns_global_state(item) for item in self._items)

    def _instrument_providers(self) -> None:
        """Auto-instrument active providers and used libraries per `Trace(instrument=...)`.

        Runs after every item is open, so provider clients exist and the
        `TracerProvider` is installed. Instruments grelmicro-managed providers
        per client, then sweeps every installed OpenTelemetry library
        instrumentor so a library the app uses through its own client (a
        SQLAlchemy or asyncpg engine, an httpx client) is traced without a
        grelmicro provider. The framework integration instruments itself
        separately in `micro.install(app)`.
        """
        component = next(
            (
                item
                for item in self.components
                if getattr(item, "kind", None) == "trace"
            ),
            None,
        )
        if component is None:
            return
        if not cast("Trace", component).active:
            # Auto-disabled Trace installs no provider, so there is nothing to
            # bind auto-instrumentation to. The directive is still validated:
            # otherwise a bad one opens cleanly in development, where no
            # endpoint is set, and first raises on the deploy that sets one.
            from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
                KNOWN_FRAMEWORKS as _KNOWN_FRAMEWORKS,
            )
            from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
                installed_instrumentors as _installed_instrumentors,
            )
            from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
                validate_directive as _validate_directive,
            )

            _validate_directive(
                cast("Trace", component).instrument,
                {provider.short_name for provider in self.providers}
                | _KNOWN_FRAMEWORKS
                | _installed_instrumentors(),
            )
            return
        from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
            KNOWN_FRAMEWORKS,
            installed_instrumentors,
            instrument_libraries,
            instrument_providers,
            provider_library_name,
            uninstrument_libraries,
            uninstrument_providers,
            validate_directive,
        )

        trace = cast("Trace", component)
        directive = trace.instrument
        providers = self.providers
        known = (
            {provider.short_name for provider in providers}
            | KNOWN_FRAMEWORKS
            | installed_instrumentors()
        )
        validate_directive(directive, known)
        # A managed provider owns its library (Redis per client, Postgres ->
        # asyncpg), and the framework integration owns FastAPI. Exclude both
        # from the sweep so the same calls are not instrumented twice.
        exclude = KNOWN_FRAMEWORKS | {
            provider_library_name(provider.short_name) for provider in providers
        }
        instrumented = instrument_providers(
            providers, trace.provider, directive
        )
        libraries = instrument_libraries(
            trace.provider, directive, exclude=exclude
        )
        if self._exit_stack is not None:  # pragma: no branch
            if instrumented:
                self._exit_stack.callback(uninstrument_providers, instrumented)
            if libraries:
                self._exit_stack.callback(uninstrument_libraries, libraries)

    async def __aenter__(self) -> Self:
        """Open every registered item in registration order.

        The active-app `ContextVar` is set before entries so items can call
        `Grelmicro.current()` from their `__aenter__`. On partial-startup
        failure, the token is reset before unwinding.
        """
        if self._exit_stack is not None:
            raise OutOfContextError(self, "__aenter__")
        with _active_apps_lock:
            if (
                not self._allow_multiple
                and self._owns_global_state()
                and any(
                    app._owns_global_state()  # noqa: SLF001
                    for app in _active_apps
                )
            ):
                raise MultipleActiveAppsError
            _active_apps.append(self)
        try:
            self._discover_shared_providers()
            self._order_providers_before_dependents()
            self._resolve_provider_sharing()
            report_unmet_requirements(
                unmet_requirements(self._items), self._environment
            )
            self._exit_stack = AsyncExitStack()
            await self._exit_stack.__aenter__()
            self._token = _current_micro.set(self)
            for item in self._items:
                await self._exit_stack.enter_async_context(item)
            self._instrument_providers()
        except BaseException:
            with _active_apps_lock:
                if self in _active_apps:  # pragma: no branch
                    _active_apps.remove(self)
            if self._token is not None:
                _current_micro.reset(self._token)
            self._token = None
            if self._exit_stack is not None:
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
            with _active_apps_lock:
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

        A `Coordination` holds three backends (a lock backend, an election
        backend, and a schedule backend), each able to borrow its own Provider,
        so all are walked and each borrowed Provider is adopted.

        Providers the user already listed are left untouched, so their declared
        order still applies and the ordering check in
        `_order_providers_before_dependents` still repairs a late listing. Adapters
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
        cache: dict[tuple[type, str], Provider] = {}
        for item in self._items:
            for target in _iter_provider_backends(item):
                if not getattr(target, "_owns_provider", False):
                    continue
                borrower = cast("_ProviderBorrower", target)
                provider = borrower._provider  # noqa: SLF001
                key = (type(provider), getattr(provider, "env_prefix", ""))
                shared = cache.get(key)
                if shared is None:
                    cache[key] = provider
                elif shared is not provider:  # pragma: no branch
                    borrower._rebind_provider(shared)  # noqa: SLF001

    def _order_providers_before_dependents(self) -> None:
        """Move a listed Provider ahead of the Component that borrows it.

        A Component built with `Coordination(provider)` borrows the provider's
        client but does not lifecycle it, and `Grelmicro.__aenter__` enters
        items in list order. A Provider listed *after* its Component would open
        too late, and one with lazy resources (`PostgresProvider` builds its
        pool on `__aenter__`) then raises `OutOfContextError`.

        A Provider absent from `uses=` is already adopted and inserted ahead of
        its Component by `_discover_shared_providers`. Leaving the listed case
        broken would mean listing a Provider is worse than omitting it, so it is
        reordered the same way. `uses=` declares what the app is made of, and
        grelmicro opens it in dependency order.

        `Grelmicro(strict=True)` still raises `LifecycleOrderError`, for callers
        who want the list they wrote to be the list that runs.
        """
        for index, item in enumerate(self._items):
            for target in _iter_provider_backends(item):
                provider = getattr(target, "_provider", None)
                if provider is None or getattr(target, "_owns_provider", True):
                    continue
                self._move_provider_ahead(target, provider, index)

    def _move_provider_ahead(
        self,
        target: object,
        provider: Provider,
        index: int,
    ) -> None:
        """Reorder one misplaced Provider, or raise under `strict=True`."""
        position = self._items.index(provider)
        if position <= index:
            return
        if self._strict:
            msg = diagnostic(
                PROVIDER_ORDER,
                f"{type(provider).__name__} is listed after "
                f"{type(target).__name__} in Grelmicro(uses=[...]). "
                f"Providers must be listed before the components that "
                f"depend on them so they open first.",
            )
            raise LifecycleOrderError(msg)
        self._items.insert(index, self._items.pop(position))


class _ProviderBorrower(Protocol):
    """A backend that holds a Provider and can be rebound onto a shared one.

    Every first-party adapter that accepts `provider=` exposes these three
    members. `Grelmicro` reads them to adopt, dedupe, and rebind Providers.
    """

    _provider: Provider
    _owns_provider: bool

    def _rebind_provider(self, provider: Provider) -> None:
        """Swap the underlying provider."""
        ...


def _iter_provider_backends(item: object) -> list[object]:
    """Return the provider-holding backends to inspect for `item`.

    Most components expose one backend via `backend`. A `Coordination`
    component holds one per entry in `COORDINATION_BACKENDS`, any of which may
    own or borrow a Provider, so all present ones are returned. A plain item
    with no backend is inspected directly.
    """
    from grelmicro.coordination._component import (  # noqa: PLC0415
        COORDINATION_BACKENDS,
    )

    backends = [
        getattr(item, f"_{slot.keyword}_backend", None)
        for slot in COORDINATION_BACKENDS
    ]
    if any(backend is not None for backend in backends):
        return [backend for backend in backends if backend is not None]
    return [getattr(item, "backend", item)]


def _sys_exc_info_or_none() -> tuple[Any, Any, Any]:
    """Return current exception triple (or three Nones if not in handler)."""
    import sys  # noqa: PLC0415

    return sys.exc_info()


def _protocol_members(protocol: type) -> frozenset[str]:
    """Return the member names a runtime-checkable Protocol matches on.

    `isinstance` against a `runtime_checkable` Protocol tests exactly these
    names and never checks signatures, so they are also what decides which of
    two matching protocols is the more specific.
    """
    return frozenset(getattr(protocol, "__protocol_attrs__", ()))


type _BackendCandidate = tuple[type, str, Callable[[Any], Component]]
"""One backend protocol, the component to name in an error, and its factory.

The factory takes `Any` because the protocol match is what proves the item
fits, and that proof is a runtime `isinstance` a type checker cannot follow.
"""


def _most_specific_backend(
    matches: list[_BackendCandidate],
    item: object,
) -> _BackendCandidate:
    """Return the match whose protocol subsumes every other match.

    A backend can satisfy more than one protocol, because `runtime_checkable`
    compares member names only. `CircuitBreakerBackend` declares everything
    `RateLimiterBackend` does plus `_loop` and `is_shared`, so every circuit
    breaker backend also matches `RateLimiterBackend`. The more specific
    protocol wins, which keeps the answer independent of the order the
    protocols are tested in.

    Raises:
        AmbiguousBackendError: If no single protocol subsumes the others, so
            the backend names two unrelated kinds and only the caller knows
            which was meant.
    """
    for candidate in matches:
        members = _protocol_members(candidate[0])
        if all(
            members >= _protocol_members(other[0])
            for other in matches
            if other[0] is not candidate[0]
        ):
            return candidate
    names = ", ".join(sorted(protocol.__name__ for protocol, _, _ in matches))
    kinds = ", ".join(sorted(label for _, label, _ in matches))
    msg = (
        f"{type(item).__name__} matches more than one backend protocol "
        f"({names}), so grelmicro cannot tell which kind it is. Wrap it in "
        f"the component you mean, one of: {kinds}."
    )
    raise AmbiguousBackendError(msg)


def _maybe_wrap_first_party_backend(item: object) -> Component | None:
    """Wrap a first-party backend in the matching Component, or return None.

    Every protocol is tested, not just the first that matches, so a backend
    satisfying two of them resolves by specificity rather than by the order
    the checks happen to be written in. See `_most_specific_backend`.

    Imports are lazy so unused submodules stay out of `import grelmicro`.
    The user importing `RedisCacheAdapter` already loads `grelmicro.cache`,
    so the lazy import here is a cache hit.

    Raises:
        AmbiguousBackendError: If the backend matches two unrelated protocols.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.cache._protocol import CacheBackend  # noqa: PLC0415
    from grelmicro.coordination._component import (  # noqa: PLC0415
        COORDINATION_BACKENDS,
        Coordination,
    )
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerComponent,
        RateLimiterComponent,
    )
    from grelmicro.resilience._protocol import (  # noqa: PLC0415
        CircuitBreakerBackend,
        RateLimiterBackend,
    )

    candidates: list[_BackendCandidate] = [
        (CacheBackend, "Cache", Cache),
        (
            CircuitBreakerBackend,
            "CircuitBreakerComponent",
            CircuitBreakerComponent,
        ),
        (RateLimiterBackend, "RateLimiterComponent", RateLimiterComponent),
        *[
            (
                slot.protocol,
                f"Coordination({slot.keyword}=...)",
                partial(Coordination._holding, slot.keyword),  # noqa: SLF001
            )
            for slot in COORDINATION_BACKENDS
        ],
    ]
    matches = [
        candidate for candidate in candidates if isinstance(item, candidate[0])
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][2](item)
    return _most_specific_backend(matches, item)[2](item)


def _unsupported_framework_message(caller: str, app: object) -> str:
    """Return the error for an app no registered integration claims.

    Names the frameworks that are actually installed, rather than a hard-coded
    list, so a missing extra reads as a missing extra.
    """
    known = integration_names()
    frameworks = ", ".join(known) if known else "none are installed"
    return (
        f"{caller} does not support {type(app).__name__!r}. "
        f"Registered integrations: {frameworks}. For an unsupported "
        f"framework, open the app in a lifespan with `async with micro:` and "
        f"add `GrelmicroMiddleware` yourself."
    )


_FAKEABLE_KINDS = frozenset(
    {"coordination", "cache", "ratelimiter", "circuitbreaker"}
)
"""Kinds `micro.fake()` swaps onto an in-process store.

Every one of these is a thin component over a backend, so replacing the
backend replaces the whole thing. `Outbox` is excluded even though memory
serves it, because it carries registered handlers and a running relay that a
swap would silently drop. `Log`, `Trace`, `Metrics`, and `HealthChecks` have
no backend to fake.
"""


def _fake_components(
    components: Iterable[Component],
    provider: Provider,
) -> list[Component]:
    """Build an in-process replacement for every fakeable component.

    Each replacement keeps the original's name, so a pattern that resolves
    `(kind, name)` finds the fake in the same slot.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.coordination._component import Coordination  # noqa: PLC0415
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerComponent,
        RateLimiterComponent,
    )

    builders: dict[str, Callable[[str], Component]] = {
        "coordination": lambda name: Coordination(provider, name=name),
        "cache": lambda name: Cache(provider, name=name),
        "ratelimiter": lambda name: RateLimiterComponent(provider, name=name),
        "circuitbreaker": lambda name: CircuitBreakerComponent(
            provider, name=name
        ),
    }
    return [
        builders[component.kind](component.name)
        for component in components
        if component.kind in _FAKEABLE_KINDS
    ]


def _default_components_for_provider(provider: Provider) -> list[Component]:
    """Build one default Component per kind `provider` serves.

    Walks the provider factories. A factory that raises `NotImplementedError`
    means the provider does not serve that kind, so the matching Component is
    skipped. `coordination` wires whichever of its backends the provider
    ships. `Outbox` is never registered this way: it carries handlers and a
    relay, so it is built where those are declared. Imports are lazy so a
    provider that serves only one kind never loads the other component
    modules.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.coordination._component import (  # noqa: PLC0415
        COORDINATION_BACKENDS,
        Coordination,
    )
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerComponent,
        RateLimiterComponent,
    )

    components: list[Component] = []

    coordination = {
        slot.keyword: _provider_backend_or_none(getattr(provider, slot.factory))
        for slot in COORDINATION_BACKENDS
    }
    if any(backend is not None for backend in coordination.values()):
        components.append(Coordination(**coordination))

    cache = _provider_backend_or_none(provider.cache)
    if cache is not None:
        components.append(Cache(cache))

    ratelimiter = _provider_backend_or_none(provider.ratelimiter)
    if ratelimiter is not None:
        components.append(RateLimiterComponent(ratelimiter))

    circuitbreaker = _provider_backend_or_none(provider.circuitbreaker)
    if circuitbreaker is not None:
        components.append(CircuitBreakerComponent(circuitbreaker))

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

    code: ClassVar[str] = PROVIDER_ORDER


class AmbientBindingError(GrelmicroError, RuntimeError):
    """Raised when ambient pattern resolution is not wired for an app.

    Covers `strict=True` with `install(ambient=False)`, which leaves ambient
    patterns unbound, and a `GrelmicroMiddleware` that another middleware
    wraps, which leaves it unable to bind before that middleware runs.

    Carries the `ambient-binding` code, the same one `AmbientBindingWarning`
    carries outside strict mode.
    """

    code: ClassVar[str] = AMBIENT_BINDING


class AmbiguousProviderError(GrelmicroError, ValueError):
    """Raised when two bare Providers in `uses=` make the default component ambiguous."""


class AmbiguousBackendError(GrelmicroError, ValueError):
    """Raised when a bare backend matches two unrelated backend protocols.

    `isinstance` against a `runtime_checkable` Protocol compares member names
    and never signatures, so one object can satisfy several. When one protocol
    subsumes the others the most specific one wins silently. When none does,
    only the caller knows which kind was meant, so wrap the backend in the
    component explicitly.
    """
