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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from typing_extensions import Doc

from grelmicro._environment import unmet_requirements
from grelmicro._redact import redact_url

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from grelmicro._app import Grelmicro
    from grelmicro._component import Component
    from grelmicro.providers._base import Provider
    from grelmicro.types import Environment

__all__ = [
    "AppReport",
    "CheckReport",
    "ComponentReport",
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


def build_report(micro: Grelmicro) -> AppReport:
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
    return AppReport(
        environment=micro.environment,
        components=components,
        providers=providers,
        checks=tuple(checks),
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


def _render(report: AppReport) -> str:
    """Render the whole report as plain text."""
    environment = report.environment or "undeclared"
    lines = [f"Environment: {environment}", ""]
    lines += _render_components(report)
    lines += _render_providers(report)
    lines.append("Checks")
    for check in report.checks:
        marker = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[check.status]
        lines.append(f"  {marker}  {check.name}: {check.detail}")
    return "\n".join(lines)
