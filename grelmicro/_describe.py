"""The wiring report behind `Grelmicro.describe()` and `grelmicro check`.

One structured answer to what got wired, from what, reachable how far, and
configured with what. `Grelmicro.describe()` returns it, `python -m grelmicro
check` renders it and turns its checks into an exit code.

The user-facing page is `docs/wiring.md`. The rules the checks apply live with
the code that owns them: backend scope in `grelmicro._environment`, ambient
binding in `Grelmicro.check_ambient_binding`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, Any, Literal

from typing_extensions import Doc

from grelmicro._diagnostics import ROUTES_NOT_DECLARED
from grelmicro._discovery import framework_of, load_integration, serves_http
from grelmicro._duration import SECOND, microseconds
from grelmicro._environment import recorded_bindings, unmet_requirements
from grelmicro._paths import matches, names_route, segment_pattern
from grelmicro._redact import redact_url
from grelmicro.providers._base import PATTERNS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import timedelta
    from re import Pattern

    from grelmicro._app import Grelmicro
    from grelmicro._component import Component
    from grelmicro._discovery import Integration
    from grelmicro.http._routes import RouteDeclaration
    from grelmicro.providers._base import Provider
    from grelmicro.types import Environment

__all__ = [
    "AppReport",
    "CheckReport",
    "ComponentReport",
    "EndpointReport",
    "ProviderReport",
]

CheckStatus = Literal["ok", "warn", "fail"]
"""How a single check came out. Only `fail` sets a non-zero exit code."""

_SECRET_HINTS = frozenset({"password", "secret", "token", "key", "auth"})
"""Field-name fragments whose value is masked whatever its type."""


@dataclass(frozen=True)
class ComponentReport:
    """One registered component, and what it resolved to."""

    kind: Annotated[str, Doc('Component category, such as `"cache"`.')]
    name: Annotated[str, Doc('Registration name, `"default"` for most.')]
    component: Annotated[str, Doc("Class name of the component itself.")]
    backends: Annotated[
        tuple[str, ...],
        Doc("Class names of the bound backends, empty when it holds none."),
    ] = ()
    provider: Annotated[
        str | None,
        Doc("Short name of the Provider the backends borrow, if any."),
    ] = None
    config: Annotated[
        Mapping[str, Any],
        Doc("Resolved configuration, with credential-like values masked."),
    ] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderReport:
    """One active Provider, and the kinds it does and does not serve."""

    short_name: Annotated[str, Doc('Vendor identifier, such as `"redis"`.')]
    provider: Annotated[str, Doc("Class name of the Provider.")]
    url: Annotated[
        str | None,
        Doc(
            "Where it connects, with the password masked. `None` if it has no URL."
        ),
    ] = None
    env_prefix: Annotated[
        str | None,
        Doc(
            """
            Environment variable prefix this Provider reads, such as
            `"REDIS_"`. Names the variables an operator sets to point it
            somewhere else.
            """,
        ),
    ] = None
    serves: Annotated[
        tuple[str, ...],
        Doc("Kinds this Provider ships an adapter for."),
    ] = ()
    declines: Annotated[
        tuple[str, ...],
        Doc(
            """
            Kinds this Provider does not serve. A component of one of these
            kinds needs its backend passed explicitly, which is the answer to
            "why is my outbox unwired".
            """,
        ),
    ] = ()


@dataclass(frozen=True)
class CheckReport:
    """One startup check and how it came out."""

    name: Annotated[str, Doc('Stable identifier, such as `"backend-scope"`.')]
    status: Annotated[CheckStatus, Doc("`ok`, `warn`, or `fail`.")]
    detail: Annotated[str, Doc("One sentence saying what was found.")]


@dataclass(frozen=True)
class EndpointReport:
    """One endpoint, and what every registered component does to it.

    Answers "what happens to `GET /products`" in one line, read from the
    routes the app declares and the components registered beside them.
    It is a view, never a second place to configure: what it says is
    computed from the one source of truth, so it cannot drift from it.
    """

    method: Annotated[str, Doc('The HTTP method, such as `"GET"`.')]
    path: Annotated[str, Doc("The path the route is declared under.")]
    applies: Annotated[
        tuple[str, ...],
        Doc(
            "What each component does to this endpoint, one entry per "
            'component, such as `"cache 60s"` or `"idempotent POST"`. '
            "Empty when nothing acts on it."
        ),
    ] = ()


@dataclass(frozen=True)
class AppReport:
    """What a `Grelmicro` app is wired with.

    Returned by `Grelmicro.describe()`. Rendered by `python -m grelmicro
    check`, which exits non-zero when `ok` is `False`.
    """

    environment: Annotated[
        Environment | None,
        Doc("The declared deployment tier, or `None`."),
    ] = None
    components: Annotated[
        tuple[ComponentReport, ...],
        Doc("Registered components, in registration order."),
    ] = ()
    providers: Annotated[
        tuple[ProviderReport, ...],
        Doc("Active Providers, in registration order."),
    ] = ()
    checks: Annotated[
        tuple[CheckReport, ...],
        Doc("Startup checks, in the order they are reported."),
    ] = ()
    endpoints: Annotated[
        tuple[EndpointReport, ...],
        Doc(
            "What each registered component does to each endpoint. Empty "
            "unless `describe(app)` was given the application, since the "
            "routes are read off it."
        ),
    ] = ()

    @property
    def ok(self) -> bool:
        """Whether every check passed. A `warn` does not fail the report."""
        return not any(check.status == "fail" for check in self.checks)

    def render(self) -> str:
        """Return the report as the text `grelmicro check` prints."""
        return _render(self)


def _mask(name: str, value: Any) -> Any:  # noqa: ANN401
    """Return `value` with credentials masked, by field name and by shape."""
    lowered = name.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "***"
    if isinstance(value, str) and "://" in value:
        return redact_url(value, multi_host=True)
    return value


def _config_of(component: object) -> Mapping[str, Any]:
    """Return a component's resolved config as a masked plain mapping.

    Reads the frozen `_config` model every component keeps. A component that
    holds none reports an empty mapping rather than failing the report.
    """
    config = getattr(component, "_config", None)
    dump = getattr(config, "model_dump", None)
    if dump is None:
        return {}
    try:
        fields = dump(mode="json")
    except TypeError, ValueError:  # pragma: no cover
        return {}
    return {name: _mask(name, value) for name, value in fields.items()}


def _backends_of(component: object) -> tuple[object, ...]:
    """Return every backend a component holds, in declaration order."""
    from grelmicro._environment import backend_attributes  # noqa: PLC0415

    found = []
    for attribute in backend_attributes():
        backend = getattr(component, attribute, None)
        if backend is not None and backend not in found:
            found.append(backend)
    return tuple(found)


def _provider_of(backends: Iterable[object]) -> str | None:
    """Return the short name of the Provider the backends borrow, if any."""
    for backend in backends:
        provider = getattr(backend, "_provider", None)
        short_name = getattr(provider, "short_name", None)
        if isinstance(short_name, str):
            return short_name
    return None


def describe_component(component: Component) -> ComponentReport:
    """Build the report entry for one registered component."""
    backends = _backends_of(component)
    return ComponentReport(
        kind=component.kind,
        name=component.name,
        component=type(component).__name__,
        backends=tuple(type(backend).__name__ for backend in backends),
        provider=_provider_of(backends),
        config=_config_of(component),
    )


def describe_provider(provider: Provider) -> ProviderReport:
    """Build the report entry for one active Provider.

    Calls each factory and reads `NotImplementedError` as "does not serve
    this kind", which is the same question `Grelmicro` asks when a bare
    Provider fills its default components.
    """
    serves: list[str] = []
    declines: list[str] = []
    for kind in PATTERNS:
        factory = getattr(provider, f"{kind}_backend")
        try:
            factory()
        except NotImplementedError:
            declines.append(kind)
        except Exception:  # noqa: BLE001
            # A factory that fails for its own reasons (no pool yet, bad
            # credentials) still ships the adapter, so the kind is served.
            serves.append(kind)
        else:
            serves.append(kind)
    # `safe_url` is the Provider's own masked form, so the report never has
    # to decide what a credential looks like for a given vendor.
    url = getattr(provider, "safe_url", None)
    env_prefix = getattr(provider, "env_prefix", None)
    return ProviderReport(
        short_name=getattr(provider, "short_name", "?"),
        provider=type(provider).__name__,
        url=url if isinstance(url, str) else None,
        env_prefix=env_prefix if isinstance(env_prefix, str) else None,
        serves=tuple(serves),
        declines=tuple(declines),
    )


def _scope_checks(
    items: Sequence[object],
    environment: Environment | None,
) -> list[CheckReport]:
    """Return the backend scope check, one entry per unmet requirement.

    Severity follows the same tier rules the startup check applies. A memory
    backend is the point of `development` and `test`, so it is reported as
    passing there. It is a failure in `staging` and `production`, and a
    warning when no tier is declared.
    """
    from grelmicro._environment import (  # noqa: PLC0415
        QUIET_ENVIRONMENTS,
        STRICT_ENVIRONMENTS,
    )

    unmet = unmet_requirements(items, recorded_bindings())
    if not unmet or environment in QUIET_ENVIRONMENTS:
        return [
            CheckReport(
                name="backend-scope",
                status="ok",
                detail=(
                    "every bound backend reaches as far as its component requires"
                    if not unmet
                    else f"unmet bindings are expected in {environment!r}"
                ),
            )
        ]
    status: CheckStatus = (
        "fail" if environment in STRICT_ENVIRONMENTS else "warn"
    )
    return [
        CheckReport(
            name="backend-scope",
            status=status,
            detail=entry.finding,
        )
        for entry in unmet
    ]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """One route the app declares, as the readers need to see it."""

    method: str
    path: str
    regex: Pattern[str]
    authenticated: bool = False
    public: bool = False
    scopes: tuple[str, ...] = ()
    declaration: RouteDeclaration | None = None


type _EndpointRules = list[tuple[str, Callable[[_Endpoint], str | None]]]
"""Each registered component's label, and what it says about one endpoint."""


SOME_PATHS = " (some paths)"
"""What a rule reaching part of one route reads as.

A pattern is matched against the URL a request asks for, and a route
template stands for many. `"/users/me"` selects one of the requests
`GET /users/{uid}` answers and not the others, so saying the rule
applies to the endpoint would overstate it and saying it does not would
be wrong. It applies to some of it.
"""


def _reach(
    endpoint: _Endpoint, include: tuple[str, ...], exclude: tuple[str, ...]
) -> str | None:
    """Return how far a component reaches into this endpoint.

    `""` for all of it, `SOME_PATHS` for part, `None` for none. The
    template answers a pattern written the way the route was, and the
    regex answers one written as a URL the route serves, which is what
    the middleware matches against.

    What `include` reaches is settled first, because `exclude` only
    narrows that. Reading `exclude` first would report a rule as
    reaching part of a route that `include` never named at all.
    """
    if not include or matches(endpoint.path, include):
        reach = ""
    elif _under(endpoint, include):
        reach = SOME_PATHS
    else:
        return None
    if matches(endpoint.path, exclude):
        return None
    return SOME_PATHS if _under(endpoint, exclude) else reach


def _under(endpoint: _Endpoint, patterns: tuple[str, ...]) -> bool:
    """Return whether a pattern names one concrete path of this route.

    One URL the route serves, not the route itself, which is what makes
    the reach partial rather than whole.
    """
    return any(
        pattern != endpoint.path
        and names_route(pattern, endpoint.path, endpoint.regex)
        for pattern in patterns
    )


def _endpoint_rules(components: Sequence[Any]) -> _EndpointRules:
    """Return what each registered component says about one endpoint.

    Each entry is the component's label and a reader that answers for one
    `(method, path)`, or `None` when the component leaves it alone. Built
    once per report rather than per route.
    """
    rules: _EndpointRules = []
    for component in components:
        kind = getattr(component, "kind", None)
        reader = _ENDPOINT_READERS.get(kind or "")
        if reader is None:
            continue
        # The default instance is the only one of its kind an app usually
        # holds, so it says what it does and stops there. A second one
        # says which it is, or two rows would read the same and mean
        # different things.
        name = component.name
        rules.append(
            ("" if name == "default" else f" ({name})", reader(component))
        )
    return rules


def _selected(config: Any, endpoint: _Endpoint) -> str | None:  # noqa: ANN401
    """Return how far a component acting on paths reaches this endpoint."""
    return _reach(endpoint, tuple(config.include), tuple(config.exclude))


def _seconds_text(duration: timedelta) -> str:
    """Write `duration` in seconds, such as `60s` or `0.5s`.

    Whole seconds are written without a fraction, and a fraction to the
    microsecond, never in exponent form.
    """
    whole, fraction = divmod(microseconds(duration), microseconds(SECOND))
    if not fraction:
        return f"{whole}s"
    return f"{whole}.{fraction:06d}".rstrip("0") + "s"


def _reads_cache(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what a `CachedResponses` does to one endpoint."""
    from grelmicro.http._routes import runs_checks  # noqa: PLC0415

    def read(endpoint: _Endpoint) -> str | None:
        state = component._live.state  # noqa: SLF001
        config = state.config
        declaration = endpoint.declaration
        # An authenticated request is answered by its handler, unless its
        # route declares one response shared among every caller it admits.
        shared = (
            declaration is not None
            and declaration.shared
            and declaration.cache is not False
        )
        if endpoint.method not in {"GET", "HEAD"} or (
            endpoint.authenticated and not shared
        ):
            return None
        if declaration is not None and (
            declaration.cache is not False or runs_checks(declaration)
        ):
            # The route's declaration decides, as it does at the route: its
            # own `cache` holds for every URL the route answers, and only
            # `exclude` takes part of it back.
            seconds = state.policies.ttl_at(
                declaration, endpoint.path, config.ttl
            )
            reach = _reach(endpoint, (), tuple(config.exclude))
        else:
            # A pattern, which may name the template or one URL under it.
            patterns = tuple(config.include)
            reach = _reach(endpoint, patterns, tuple(config.exclude))
            seconds = _pattern_seconds(state, config, endpoint, patterns)
        if reach is None or seconds is None:
            return None
        return f"cache {_seconds_text(seconds)}{reach}"

    return read


def _pattern_seconds(
    state: Any,  # noqa: ANN401
    config: Any,  # noqa: ANN401
    endpoint: _Endpoint,
    patterns: tuple[str, ...],
) -> timedelta | None:
    """Return how long a pattern keeps this route for, or `None`.

    The template first, which is the pattern written the way the route
    was. Then the URLs it serves, because a pattern may name one of
    those and the middleware matches against the request.
    """
    seconds = state.policies.pattern_ttl(endpoint.path, config.ttl)
    if seconds is not None:
        return seconds
    for pattern in patterns:
        if names_route(pattern, endpoint.path, endpoint.regex):
            return state.policies.pattern_ttl(pattern, config.ttl)
    return None


def _reads_conditional(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what a `ConditionalRequests` does to one endpoint."""

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        reach = _selected(config, endpoint)
        if reach is None:
            return None
        required = {name.upper() for name in config.require_precondition}
        applied = (
            "conditional required"
            if endpoint.method in required
            else "conditional"
        )
        return f"{applied}{reach}"

    return read


def _reads_idempotent(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what an `IdempotentRequests` does to one endpoint."""
    from grelmicro.http._routes import runs_checks  # noqa: PLC0415

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        methods = {name.upper() for name in config.methods}
        reach = _selected(config, endpoint)
        if endpoint.method not in methods or reach is None:
            return None
        if component._key_function is None and (  # noqa: SLF001
            endpoint.authenticated
            or (
                endpoint.declaration is not None
                and runs_checks(endpoint.declaration)
            )
        ):
            return None
        window = component.idempotency.config.ttl
        return f"idempotent {_seconds_text(window)}{reach}"

    return read


def _reads_authenticated(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what an `AuthenticatedRequests` does to one endpoint."""

    def read(endpoint: _Endpoint) -> str | None:
        reach = _reach(endpoint, (), tuple(component.config.exclude))
        if reach is None:
            return None
        if endpoint.public:
            return "anonymous"
        scopes = " ".join(endpoint.scopes)
        return (
            f"authenticated {scopes}{reach}"
            if scopes
            else f"authenticated{reach}"
        )

    return read


def _authenticated_by(components: Sequence[Any], endpoint: _Endpoint) -> bool:
    """Return whether an authentication middleware covers this endpoint.

    A covered request carries a caller, so the cache answers it from its
    handler and an idempotent replay is skipped.
    """
    return any(
        _reach(endpoint, (), tuple(component.config.exclude)) is not None
        and not endpoint.public
        for component in components
    )


def _authentication_of(
    app: object,
) -> Callable[[str, str], tuple[bool, tuple[str, ...]]]:
    """Return what says how a route is authenticated, from its declaration.

    Asked with the route's path and a method, it answers whether the route
    is served without a credential, and the scopes it requires. Read off
    the declarations the app's integration lists, never off the running
    middleware, so a report on one app changes nothing another one serves.
    An authentication the app added by hand serves no route publicly.
    """
    from grelmicro.http._authentication import (  # noqa: PLC0415
        route_authentication,
        serves_anonymous_routes,
    )

    read = route_authentication(app)
    if serves_anonymous_routes(app):
        return read

    def protected(path: str, method: str) -> tuple[bool, tuple[str, ...]]:
        _, scopes = read(path, method)
        return False, scopes

    return protected


def _reads_rate_limited(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what a `RateLimitedRequests` does to one endpoint."""

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        parts = []
        route_reach = _selected(config, endpoint)
        if route_reach is not None:
            named = ", ".join(limiter.name for limiter in component.limiters)
            parts.append(f"{named}{route_reach}")
        flood = component.flood
        if flood is not None:
            # Only `exclude` narrows the flood limit, which runs before routing.
            flood_reach = _reach(endpoint, (), tuple(config.exclude))
            if flood_reach is not None:
                parts.append(f"flood={flood.name}{flood_reach}")
        return f"rate-limit {', '.join(parts)}" if parts else None

    return read


def _reads_access_log(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what an `AccessLog` does to one endpoint."""

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        reach = _selected(config, endpoint)
        if reach is None:
            return None
        quiet = matches(endpoint.path, tuple(config.quiet))
        applied = "access-log quiet" if quiet else "access-log"
        return f"{applied}{reach}"

    return read


_ENDPOINT_READERS: Mapping[
    str, Callable[[Any], Callable[[_Endpoint], str | None]]
] = {
    "authenticated_requests": _reads_authenticated,
    "cached_responses": _reads_cache,
    "conditional_requests": _reads_conditional,
    "idempotent_requests": _reads_idempotent,
    "rate_limited_requests": _reads_rate_limited,
    "access_log": _reads_access_log,
}
"""Which components answer for an endpoint, and how to read each.

A component absent from here does nothing per endpoint, so it is left out
of the view rather than reported as doing nothing.
"""


@dataclass(frozen=True, slots=True)
class _Routes:
    """The app's integration, and the routes it lists.

    Each declared path comes with the regex a URL is matched against: a
    parameter matches one path segment, and a `path` parameter every
    segment left.
    """

    integration: Integration | None
    listed: tuple[RouteDeclaration, ...]
    patterns: Mapping[str, Pattern[str]]

    @classmethod
    def of(cls, app: object) -> _Routes:
        """Load the integration of `app` and read its routes, compiling each declared path once."""
        from grelmicro.http._routes import routes_listed_by  # noqa: PLC0415

        integration = load_integration(app)
        listed = tuple(routes_listed_by(integration, app))
        paths = dict.fromkeys(declaration.path for declaration in listed)
        patterns = {path: segment_pattern(path) for path in paths}
        return cls(integration, listed, patterns)


def _describe_endpoints(
    micro: Grelmicro,
    app: object,
    routes: _Routes,
    rules: _EndpointRules,
) -> tuple[EndpointReport, ...]:
    """Return one row per method the app's routes declare, in path order.

    The routes are the ones the app's integration lists, mounted and
    wrapped ones included. A route declared for every method, such as a
    mount or a websocket route, has no row.
    """
    from grelmicro.http._routes import DeclaredRoutes  # noqa: PLC0415

    if not rules:
        return ()
    authenticating = [
        component for component in micro.components if _authenticates(component)
    ]
    authentication = _authentication_of(app) if authenticating else None
    declared = DeclaredRoutes(routes.listed)
    found: list[EndpointReport] = []
    for listing in routes.listed:
        path = listing.path
        for method in sorted(listing.methods or ()):
            if method == "HEAD":
                continue
            endpoint = _Endpoint(
                method=method,
                path=path,
                regex=routes.patterns[path],
                declaration=declared.of(path, method),
            )
            if authentication is not None:
                public, scopes = authentication(path, method)
                endpoint = replace(endpoint, public=public, scopes=scopes)
            endpoint = replace(
                endpoint,
                authenticated=_authenticated_by(authenticating, endpoint),
            )
            found.append(
                EndpointReport(
                    method=method,
                    path=path,
                    applies=tuple(
                        f"{applied}{label}"
                        for label, read in rules
                        if (applied := read(endpoint)) is not None
                    ),
                )
            )
    return tuple(sorted(found, key=lambda row: (row.path, row.method)))


def _served_paths(routes: _Routes) -> list[tuple[str, Pattern[str]]]:
    """Return every path the app's routes serve, as a template and its regex.

    The template answers a pattern written the way the route was, and
    the regex answers one written as a URL the route serves, one path
    segment per parameter. A route declared for every method, such as a
    mount, also serves every path under it.
    """
    found: list[tuple[str, Pattern[str]]] = []
    for declaration in routes.listed:
        path = declaration.path
        found.append((path, routes.patterns[path]))
        if declaration.methods is None:
            under = f"{path.rstrip('/')}/{{path:path}}"
            found.append((under, segment_pattern(under)))
    return found


def _names_any(pattern: str, declared: list[tuple[str, Pattern[str]]]) -> bool:
    """Return whether any declared route could be selected by `pattern`.

    One question, one answer: the same `names_route` the response cache
    refuses a gated read with, so the check and the refusal can never
    read a pattern differently.
    """
    return any(
        names_route(pattern, template, regex) for template, regex in declared
    )


def _pattern_checks(micro: Grelmicro, routes: _Routes) -> list[CheckReport]:
    """Report a path pattern that names none of the app's routes.

    A pattern naming a concrete path under a parameterized route counts
    as matched: `"/users/me"` names `GET /users/{uid}`. Every route is
    read, including the ones that declare no method and so have no row in
    the table. A path under a route declared for every method, such as a
    mount, counts as matched. So does a path an `AuthenticatedRequests`
    publishes its protected resource metadata at.

    Only `include` patterns are checked, each as a warning.
    """
    declared = _served_paths(routes) + _metadata_paths(micro)
    if not declared:
        return []
    found: list[CheckReport] = []
    for entry in micro.components:
        if getattr(entry, "kind", "") not in _ENDPOINT_READERS:
            continue
        component: Any = entry
        config = component.config
        # `include` only. A component with no `include` at all, such as
        # `AuthenticatedRequests`, has none.
        patterns = tuple(getattr(config, "include", ()))
        missing = sorted(
            pattern for pattern in patterns if not _names_any(pattern, declared)
        )
        if missing:
            found.append(
                CheckReport(
                    name="path-patterns",
                    status="warn",
                    detail=(
                        f"{component.kind}/{component.name} names "
                        f"{', '.join(missing)}, which no route matches"
                    ),
                )
            )
    return found


def _metadata_paths(micro: Grelmicro) -> list[tuple[str, Pattern[str]]]:
    """Return each path a registered `AuthenticatedRequests` serves its metadata at."""
    found: list[tuple[str, Pattern[str]]] = []
    for entry in micro.components:
        if not _authenticates(entry):
            continue
        component: Any = entry
        found.extend(
            (path, segment_pattern(path))
            for path in sorted(component._metadata_paths())  # noqa: SLF001
        )
    return found


def _authenticates(component: object) -> bool:
    """Return whether `component` is an `AuthenticatedRequests`."""
    return getattr(component, "kind", None) == "authenticated_requests"


def _routes_check(
    app: object, routes: _Routes, rules: _EndpointRules
) -> list[CheckReport]:
    """Report an app whose integration serves HTTP and lists no route.

    Only when a registered component acts per endpoint.
    """
    integration = routes.integration
    if (
        not rules
        or not serves_http(integration)
        or hasattr(integration, "route_declarations")
    ):
        return []
    return [
        CheckReport(
            name=ROUTES_NOT_DECLARED,
            status="warn",
            detail=(
                f"the {framework_of(app)} integration has no "
                f"route_declarations, so no endpoint is listed and no "
                f"include= pattern is checked. Ship route_declarations in "
                f"the integration."
            ),
        )
    ]


def build_report(micro: Grelmicro, app: object = None) -> AppReport:
    """Build the full report for `micro`.

    Reads only what is already registered, so it is safe before the app is
    open as well as while it runs. The app's routes are read once.
    """
    components = tuple(
        describe_component(component) for component in micro.components
    )
    providers = tuple(
        describe_provider(provider) for provider in micro.providers
    )
    checks = _scope_checks(list(micro.components), micro.environment)
    endpoints: tuple[EndpointReport, ...] = ()
    if app is not None:
        routes = _Routes.of(app)
        rules = _endpoint_rules(list(micro.components))
        endpoints = _describe_endpoints(micro, app, routes, rules)
        checks += _pattern_checks(micro, routes) + _routes_check(
            app, routes, rules
        )
    return AppReport(
        environment=micro.environment,
        components=components,
        providers=providers,
        checks=tuple(checks),
        endpoints=endpoints,
    )


def _render_components(report: AppReport) -> list[str]:
    """Return the Components block, aligned on the longest label."""
    if not report.components:
        return ["Components", "  none registered", ""]
    labels = [f"{c.kind}/{c.name}" for c in report.components]
    width = max(len(text) for text in labels)
    lines = ["Components"]
    for text, component in zip(labels, report.components, strict=True):
        backends = ", ".join(component.backends) or component.component
        suffix = f" <- {component.provider}" if component.provider else ""
        lines.append(f"  {text.ljust(width)}  {backends}{suffix}")
    lines.append("")
    return lines


def _render_providers(report: AppReport) -> list[str]:
    """Return the Providers block, naming served and declined kinds."""
    if not report.providers:
        return []
    lines = ["Providers"]
    for provider in report.providers:
        where = f"  {provider.url}" if provider.url else ""
        lines.append(f"  {provider.short_name}  ({provider.provider}){where}")
        if provider.env_prefix:
            lines.append(f"    env:      {provider.env_prefix}*")
        lines.append(f"    serves:   {', '.join(provider.serves) or 'nothing'}")
        if provider.declines:
            lines.append(f"    declines: {', '.join(provider.declines)}")
    lines.append("")
    return lines


def _render_endpoints(report: AppReport) -> list[str]:
    """Return the Endpoints block, aligned on the longest route."""
    if not report.endpoints:
        return []
    labels = [f"{row.method:<6} {row.path}" for row in report.endpoints]
    width = max(len(text) for text in labels)
    lines = ["Endpoints"]
    for text, row in zip(labels, report.endpoints, strict=True):
        applied = "  ".join(row.applies) or "-"
        lines.append(f"  {text.ljust(width)}  {applied}")
    lines.append("")
    return lines


def _render(report: AppReport) -> str:
    """Render the whole report as plain text."""
    environment = report.environment or "undeclared"
    lines = [f"Environment: {environment}", ""]
    lines += _render_components(report)
    lines += _render_providers(report)
    lines += _render_endpoints(report)
    lines.append("Checks")
    for check in report.checks:
        marker = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[check.status]
        lines.append(f"  {marker}  {check.name}: {check.detail}")
    return "\n".join(lines)
