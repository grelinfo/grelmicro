"""Pending behavior changes, renames and removals, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached, or the
`load()` of a `Moved` entry from the module `__getattr__`. Each one warns
with the `GrelmicroFutureWarning` subclass of the release that switches
it. A test fails once the changelog reaches the release that should switch
the default or remove the old name.
"""

import importlib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from grelmicro.errors import Grelmicro045Warning, GrelmicroFutureWarning

RELEASE_WARNINGS: dict[str, type[GrelmicroFutureWarning]] = {
    category.switch_in: category for category in (Grelmicro045Warning,)
}
"""The `GrelmicroFutureWarning` subclass of each switching release, by release."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FutureChange:
    """A default that changes in `switch_in`.

    Warn only while the argument is left at its default.
    """

    change: str
    """What changes, as a sentence with no period.

    Such as "`Cron(gate=)` defaults to `leader`".
    """

    switch_in: str
    """The release that switches the default, as `MAJOR.MINOR.PATCH`."""

    keep: str | None
    """The setting that keeps the current behavior, such as `gate=None`.

    None when no setting keeps it, so the message offers only `adopt`.
    """

    adopt: str
    """The setting that adopts the new behavior now."""

    verb: Literal["Pass", "Set", "Compare"] = "Pass"
    """How the message asks for `keep` and `adopt`.

    "Pass" for an argument, "Set" for an environment variable, and
    "Compare" for a value read back.
    """

    narrow: type[GrelmicroFutureWarning] | None = None
    """A subclass of the release's category for this change alone.

    None warns with the release's category.
    """

    remedy: str | None = None
    """A sentence that replaces the `keep` and `adopt` advice, with its period.

    For a fix that no single setting names. None words the advice from
    `keep` and `adopt`.
    """

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits."""
        return self.narrow or RELEASE_WARNINGS[self.switch_in]

    @property
    def message(self) -> str:
        """The warning message."""
        if self.remedy is not None:
            return f"{self.change} from {self.switch_in}. {self.remedy}"
        if self.keep is None:
            return (
                f"{self.change} from {self.switch_in}. "
                f"{self.verb} `{self.adopt}` now."
            )
        return (
            f"{self.change} from {self.switch_in}. "
            f"{self.verb} `{self.keep}` to keep the current behavior, "
            f"or `{self.adopt}` to adopt the new one now."
        )

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Rename:
    """A public name replaced by `new`, with `old` removed in `removed_in`."""

    old: str
    """The name being retired."""

    new: str
    """The name that replaces it."""

    removed_in: str
    """The release that removes `old`, as `MAJOR.MINOR.PATCH`."""

    @property
    def message(self) -> str:
        """The warning message."""
        return (
            f"`{self.old}` is renamed to `{self.new}`. "
            f"`{self.old}` is removed in {self.removed_in}."
        )

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Removal:
    """A public name removed in `removed_in`, with `use` in its place."""

    old: str
    """The name being retired."""

    use: str
    """What to use instead, as a phrase with no period.

    Such as "`\"prometheus\"` and the `MetricsExporter` type".
    """

    removed_in: str
    """The release that removes `old`, as `MAJOR.MINOR.PATCH`."""

    @property
    def message(self) -> str:
        """The warning message."""
        return (
            f"`{self.old}` is removed in {self.removed_in}. "
            f"Use {self.use} instead."
        )

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Moved:
    """Names that `module` serves until `removed_in`, each from its home.

    `module` loads each name from its home on access and warns. The names
    stay out of its `__all__` and `dir()`.
    """

    module: str
    """The module the names leave, such as `grelmicro.integrations.fastapi`."""

    homes: Mapping[str, str] = field(repr=False)
    """The module each name loads from, by name.

    A home with a leading underscore in any part is private, and the message
    says so rather than asking for an import from it.
    """

    removed_in: str
    """The release that removes the names, as `MAJOR.MINOR.PATCH`."""

    @property
    def change(self) -> str:
        """What changes, as a phrase with no period."""
        return f"names `{self.module}` served without listing them"

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `load` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def message_for(self, name: str) -> str:
        """Return the warning message for `name`."""
        home = self.homes[name]
        moves = f"`{name}` moves out of `{self.module}` in {self.removed_in}."
        if any(part.startswith("_") for part in home.split(".")):
            return f"{moves} It is private and lives in `{home}`."
        return f"{moves} Import it from `{home}` instead."

    def load(self, name: str, stacklevel: int = 2) -> object:
        """Return `name` from its home and emit a `category` warning.

        Raise `AttributeError` for a name `module` never served.
        `stacklevel` counts from the function that calls `load`, so the
        default reports the line that called that function.
        """
        home = self.homes.get(name)
        if home is None:
            msg = f"module {self.module!r} has no attribute {name!r}"
            raise AttributeError(msg)
        value = getattr(importlib.import_module(home), name)
        warnings.warn(
            self.message_for(name), self.category, stacklevel=stacklevel + 1
        )
        return value


ENV_LOAD_DEFAULT_WARNING = Removal(
    old="EnvLoadDefaultWarning",
    use="`EnvLoadOffWarning`",
    removed_in="0.45.0",
)
"""`EnvLoadDefaultWarning`, no longer emitted since `GREL_*` is read by default."""


FORMAT_CASE = FutureChange(
    change='`LogConfig.format` reads back in lowercase, such as `"json"` '
    'rather than `"JSON"`',
    switch_in="0.45.0",
    keep=None,
    adopt='config.format.lower() == "json"',
    verb="Compare",
)
"""A built-in `LogConfig.format` reads back in lowercase."""


def format_case(name: str) -> FutureChange:
    """Return `FORMAT_CASE` worded for the built-in format `name`.

    `name` is the format in lowercase, such as `json`.
    """
    return replace(
        FORMAT_CASE,
        change=f'`LogConfig.format` reads back as `"{name}"` '
        f'rather than `"{name.upper()}"`',
        adopt=f'config.format.lower() == "{name}"',
    )


PROVIDER_CHECKS_NONE = Removal(
    old="provider_checks=None",
    use='`provider_checks="off"` to register no provider checks, '
    'or omit the argument for `"report"`',
    removed_in="0.45.0",
)
"""`HealthChecks(provider_checks=None)`, which reads as `"report"`."""


GATE_BACKEND_STRICT = FutureChange(
    change="`gate-backend` fails `grelmicro check` in `staging` and "
    "`production`",
    switch_in="0.45.0",
    keep=None,
    adopt="backend=",
    remedy="Only a gated task with no backend is affected, and the running "
    "app is not. Register a `Coordination` with a lock or schedule backend, "
    "or pass `backend=` to the task.",
)
"""A gated task that resolves no backend fails the check in a strict tier."""


FASTAPI_MOVED = Moved(
    module="grelmicro.integrations.fastapi",
    homes={
        "ErrorResponses": "grelmicro.http",
        "JWTClaims": "grelmicro.security",
        "Metrics": "grelmicro.metrics",
        "PROMETHEUS_MEDIA_TYPE": "grelmicro.metrics._endpoints",
        "PreconditionRequiredError": "grelmicro.http",
        "Principal": "grelmicro.security",
        "RateLimitExceededError": "grelmicro.resilience",
        "RateLimiter": "grelmicro.resilience",
        "Requirement": "grelmicro.http._requirement",
        "Trace": "grelmicro.trace",
        "TrustedProxies": "grelmicro.security",
        "VerifiedToken": "grelmicro.security",
        "add_error_schema": "grelmicro.http._openapi",
        "bucket_of": "grelmicro.http._ratelimit",
        "check_freshness": "grelmicro.http",
        "check_route_limiters": "grelmicro.http._ratelimit",
        "declarations_of": "grelmicro.integrations._route_gate",
        "declare_anonymous": "grelmicro.http._authentication",
        "declare_cached": "grelmicro.http._response_cache",
        "declare_from_request": "grelmicro.http._requirement",
        "declare_optional": "grelmicro.http._requirement",
        "declare_precondition_required": "grelmicro.http._conditional",
        "describe_app_schema": "grelmicro.http._authentication",
        "describing": "grelmicro.http._openapi",
        "exceptions_on_spans": "grelmicro.integrations._request_telemetry",
        "excluding": "grelmicro.integrations._request_telemetry",
        "gate_routes": "grelmicro.integrations._route_gate",
        "is_authenticated": "grelmicro._caller",
        "is_selected": "grelmicro.trace._autoinstrument",
        "normal_close": "grelmicro.integrations._request_telemetry",
        "referenced": "grelmicro.http._openapi",
        "render_prometheus": "grelmicro.metrics._endpoints",
        "requirement_for": "grelmicro.http._requirement",
        "shadowed_routes": "grelmicro.integrations._route_gate",
        "spend": "grelmicro.http._ratelimit",
        "state_on": "grelmicro.http._ratelimit",
    },
    removed_in="0.45.0",
)
"""The names `grelmicro.integrations.fastapi` served in 0.43 without listing them."""


STARLETTE_MOVED = Moved(
    module="grelmicro.integrations.starlette",
    homes={
        "AUTHENTICATED": "grelmicro.http._requirement",
        "BODYLESS_STATUSES": "grelmicro.http._kinds",
        "ErrorResponses": "grelmicro.http",
        "HANDLED": "grelmicro.http._kinds",
        "RateLimitMiddleware": "grelmicro.http",
        "Requirement": "grelmicro.http._requirement",
        "VerifiedToken": "grelmicro.security",
        "declarations_of": "grelmicro.integrations._route_gate",
        "declared_scopes": "grelmicro.http._requirement",
        "gate_routes": "grelmicro.integrations._route_gate",
        "merge_headers": "grelmicro.http",
        "refuse_registered": "grelmicro._wrapping",
        "request_spans": "grelmicro.trace._autoinstrument",
        "shadowed_routes": "grelmicro.integrations._route_gate",
    },
    removed_in="0.45.0",
)
"""The names `grelmicro.integrations.starlette` served in 0.43 without listing them."""


LITESTAR_MOVED = Moved(
    module="grelmicro.integrations.litestar",
    homes={
        "ANONYMOUS_OPT": "grelmicro.http._authentication",
        "AUTHENTICATED": "grelmicro.http._requirement",
        "AuthenticatedRequestsMiddleware": "grelmicro.http",
        "BODYLESS_STATUSES": "grelmicro.http._kinds",
        "ErrorResponses": "grelmicro.http",
        "HANDLED": "grelmicro.http._kinds",
        "METADATA_MARKER": "grelmicro.http._authentication",
        "RateLimitMiddleware": "grelmicro.http",
        "Requirement": "grelmicro.http._requirement",
        "RouteDeclaration": "grelmicro.http",
        "add_error_schema": "grelmicro.http._openapi",
        "add_openapi_edit": "grelmicro.integrations._litestar_openapi",
        "declared_scopes": "grelmicro.http._requirement",
        "describe_app_schema": "grelmicro.http._authentication",
        "describing": "grelmicro.http._openapi",
        "merge_headers": "grelmicro.http",
        "read_route": "grelmicro._paths",
        "refuse_anonymous_caller": "grelmicro.http._authentication",
        "refuse_routes_at_metadata": "grelmicro.http._authentication",
        "request_spans": "grelmicro.trace._autoinstrument",
        "resource_metadata_of": "grelmicro.http._authentication",
        "template_under_root": "grelmicro.http._authentication",
    },
    removed_in="0.45.0",
)
"""The names `grelmicro.integrations.litestar` served in 0.43 without listing them."""


SECURITY_MOVED: tuple[Moved, ...] = (
    Moved(
        module="grelmicro.security.bans",
        homes={"AdmissionError": "grelmicro.errors"},
        removed_in="0.45.0",
    ),
    Moved(
        module="grelmicro.security.jwt",
        homes={"GrelmicroError": "grelmicro.errors"},
        removed_in="0.45.0",
    ),
)
"""Error classes `grelmicro.security.bans` and `.jwt` served in 0.43."""


PENDING: tuple[FutureChange | Rename | Removal | Moved, ...] = (
    GATE_BACKEND_STRICT,
    ENV_LOAD_DEFAULT_WARNING,
    FORMAT_CASE,
    PROVIDER_CHECKS_NONE,
    FASTAPI_MOVED,
    STARLETTE_MOVED,
    LITESTAR_MOVED,
    *SECURITY_MOVED,
)
"""Every behavior change, rename and removal that currently warns."""


FACADE_MOVES: Mapping[str, Moved] = {
    entry.module: entry for entry in PENDING if isinstance(entry, Moved)
}
"""Each pending `Moved` entry, by the module the names leave."""
