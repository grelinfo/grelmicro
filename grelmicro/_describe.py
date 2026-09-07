"""The wiring report behind `Grelmicro.describe()` and `grelmicro check`.

One structured answer to what got wired, from what, reachable how far, and
configured with what. `Grelmicro.describe()` returns it, `python -m grelmicro
check` renders it and turns its checks into an exit code.

The user-facing page is `docs/wiring.md`. The rules the checks apply live with
the code that owns them: backend scope in `grelmicro._environment`, ambient
binding in `Grelmicro.check_ambient_binding`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from typing_extensions import Doc

from grelmicro._environment import unmet_requirements
from grelmicro._paths import matches, walk_routes
from grelmicro._redact import redact_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from re import Pattern

    from grelmicro._app import Grelmicro
    from grelmicro._component import Component
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

_PROVIDER_KINDS: tuple[str, ...] = (
    "lock",
    "readwritelock",
    "leaderelection",
    "schedule",
    "cache",
    "outbox",
    "ratelimiter",
    "circuitbreaker",
)
"""Every kind a `Provider` may serve, in the order the report lists them.

A factory that raises `NotImplementedError` means the Provider does not serve
that kind. That answer is invisible at runtime today, which is what makes
`uses=[redis]` leaving the outbox unwired hard to diagnose.
"""

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
    except (TypeError, ValueError):  # pragma: no cover
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
    for kind in _PROVIDER_KINDS:
        factory = getattr(provider, kind, None)
        if factory is None:  # pragma: no cover
            continue
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

    unmet = unmet_requirements(items)
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
            detail=(
                f"{entry.component} is bound to {entry.backend}, which "
                f"{entry.provides} scope {entry.scope!r}, but requires "
                f"{entry.requires!r}"
            ),
        )
        for entry in unmet
    ]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """One route the app declares, as the readers need to see it."""

    method: str
    path: str
    route: Any
    contexts: tuple[Any, ...]
    regex: Any = None


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
    """Return whether a pattern names one concrete path of this route."""
    regex = endpoint.regex
    if regex is None:
        return False
    return any(
        not pattern.endswith("*") and regex.fullmatch(pattern)
        for pattern in patterns
    )


def _endpoint_rules(
    components: Sequence[Any],
) -> list[tuple[str, Callable[[_Endpoint], str | None]]]:
    """Return what each registered component says about one endpoint.

    Each entry is the component's label and a reader that answers for one
    `(method, path)`, or `None` when the component leaves it alone. Built
    once per report rather than per route.
    """
    rules: list[tuple[str, Callable[[_Endpoint], str | None]]] = []
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


def _reads_cache(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what a `CachedResponses` does to one endpoint.

    The route's own declaration is read off the route, not matched
    against its path. A declared path is a pattern rather than a URL, so
    matching `/products/{pid:int}` against the regex compiled from it
    answers `no` and the endpoint would be reported as uncached.
    """
    from grelmicro.http._response_cache import declared_ttl  # noqa: PLC0415

    def read(endpoint: _Endpoint) -> str | None:
        state = component._live.state  # noqa: SLF001
        config = state.config
        if endpoint.method not in {"GET", "HEAD"} or matches(
            endpoint.path, config.exclude
        ):
            return None
        declared, ttl = declared_ttl(
            endpoint.route, endpoint.contexts, endpoint.path
        )
        seconds = (
            (config.ttl if ttl is None else ttl)
            if declared
            else state.policies.pattern_ttl(endpoint.path, config.ttl)
        )
        return None if seconds is None else f"cache {seconds:g}s"

    return read


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

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        methods = {name.upper() for name in config.methods}
        reach = _selected(config, endpoint)
        if endpoint.method not in methods or reach is None:
            return None
        window = component.idempotency.config.ttl
        return f"idempotent {window:g}s{reach}"

    return read


def _reads_rate_limited(component: Any) -> Callable[[_Endpoint], str | None]:  # noqa: ANN401
    """Return what a `RateLimitedRequests` does to one endpoint."""

    def read(endpoint: _Endpoint) -> str | None:
        config = component.config
        reach = _selected(config, endpoint)
        if reach is None:
            return None
        named = ", ".join(limiter.name for limiter in component.limiters)
        return f"rate-limit {named}{reach}"

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


def _describe_endpoints(
    micro: Grelmicro,
    app: object,
) -> tuple[EndpointReport, ...]:
    """Return one row per endpoint the app declares, in path order.

    Reads the routes off the application the same way the response cache
    reads them, so a mounted app and an included router are walked too.
    """
    rules = _endpoint_rules(list(micro.components))
    if not rules:
        return ()
    compiled = dict(_declared_paths(app))
    found: list[EndpointReport] = []
    for prefix, route, contexts in walk_routes(app):
        path = f"{prefix}{getattr(route, 'path', '')}"
        for method in sorted(getattr(route, "methods", ()) or ()):
            if method == "HEAD":
                continue
            endpoint = _Endpoint(
                method=method,
                path=path,
                route=route,
                contexts=contexts,
                regex=compiled.get(path),
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


def _declared_paths(app: object) -> list[tuple[str, Pattern[str] | None]]:
    """Return every route the app declares, as its template and its regex.

    The template answers a pattern written the way the route was, and
    the regex answers one written as a URL the route serves.

    A template no compiler here understands contributes its template and
    no regex. Starlette owns the compiler and is not a dependency of
    grelmicro, and a framework that is not Starlette spells a converter
    its own way, so a report on a Litestar app must come back with what
    it could read rather than not come back at all.
    """
    try:
        from starlette.routing import compile_path  # noqa: PLC0415
    except ImportError:  # pragma: no cover - starlette is in the test env
        compile_path = None  # type: ignore[assignment]
    found: list[tuple[str, Pattern[str] | None]] = []
    for prefix, route, _ in walk_routes(app):
        template = f"{prefix}{getattr(route, 'path', '')}"
        found.append((template, _compiled(compile_path, template)))
    return found


def _compiled(
    compile_path: Any,  # noqa: ANN401
    template: str,
) -> Pattern[str] | None:
    """Return the regex this template matches with, or `None` for neither.

    Every failure is the same answer: the template stands for itself and
    nothing is read into it. A converter another framework declares is
    an `AssertionError` here rather than an exception of its own, which
    is why this catches broadly.
    """
    if compile_path is None:
        return None
    try:
        regex, _, _ = compile_path(template)
    except Exception:  # noqa: BLE001
        return None
    return cast("Pattern[str]", regex)


def _names_a_route(
    pattern: str, declared: list[tuple[str, Pattern[str] | None]]
) -> bool:
    """Return whether any declared route could be selected by `pattern`.

    A prefix pattern needs a route sitting under it, which the template
    answers. An exact one needs a route it names, which the template
    answers when it was written the same way and the regex answers when
    it was written as a URL.
    """
    if pattern.endswith("*"):
        under = pattern[:-1]
        return any(
            template.startswith(under) or template == under.rstrip("/")
            for template, _ in declared
        )
    return any(
        template == pattern or (regex is not None and regex.fullmatch(pattern))
        for template, regex in declared
    )


def _pattern_checks(
    micro: Grelmicro,
    app: object,
) -> list[CheckReport]:
    """Report a path pattern that names none of the app's routes.

    A pattern is a string, and a mistyped one turns a rule off without
    saying so: nothing matches, so nothing happens, and the endpoint
    table shows no row for it because rows come from routes. This is
    where it becomes visible.

    A pattern naming a concrete path under a parameterized route counts
    as matched, because at runtime it is matched against the URL and not
    against the template: `"/users/me"` does select the request that
    `GET /users/{uid}` answers. Every route is read, including the ones
    that declare no method and so have no row in the table.

    A warning rather than a failure. A router mounted after `install` is
    legitimate, and so is a pattern written for a path another service
    behind the same prefix serves.
    """
    declared = _declared_paths(app)
    if not declared:
        return []
    found: list[CheckReport] = []
    for entry in micro.components:
        if getattr(entry, "kind", "") not in _ENDPOINT_READERS:
            continue
        component: Any = entry
        config = component.config
        patterns = (*config.include, *config.exclude)
        missing = sorted(
            pattern
            for pattern in patterns
            if not _names_a_route(pattern, declared)
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


def build_report(micro: Grelmicro, app: object = None) -> AppReport:
    """Build the full report for `micro`.

    Reads only what is already registered, so it is safe before the app is
    open as well as while it runs.
    """
    components = tuple(
        describe_component(component) for component in micro.components
    )
    providers = tuple(
        describe_provider(provider) for provider in micro.providers
    )
    checks = _scope_checks(list(micro.components), micro.environment)
    endpoints = () if app is None else _describe_endpoints(micro, app)
    if app is not None:
        checks += _pattern_checks(micro, app)
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
