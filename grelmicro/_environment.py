"""Deployment environment and the backend scope check.

Exposes:

- `resolve_environment`: read the declared tier from an argument or
  `GREL_ENVIRONMENT`.
- `unmet_requirements`: walk registered items and return every backend whose
  scope falls short of what its component requires.
- `report_unmet_requirements`: raise or warn, by declared tier.

The user-facing rules are in `docs/deployment.md`, the model in
`docs/architecture/backends.md`.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, get_args

from grelmicro._config import defer_report
from grelmicro.errors import BackendScopeError, GrelmicroConfigWarning
from grelmicro.types import BackendScope, Environment

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger("grelmicro")

ENVIRONMENT_VAR: Final = "GREL_ENVIRONMENT"
"""Variable naming the tier, read whatever `GREL_ENV_LOAD` says.

The flag gates the variables that fill component fields. This one selects the
severity of the backend scope check, so it is read either way.
"""

ENVIRONMENTS: Final[tuple[Environment, ...]] = get_args(Environment.__value__)
"""The tiers that gate, in the order the messages list them."""

STRICT_ENVIRONMENTS: Final = frozenset({"staging", "production"})
"""Tiers where an unmet requirement is an error instead of a warning."""

QUIET_ENVIRONMENTS: Final = frozenset({"development", "test"})
"""Tiers that report nothing."""

BACKEND_ATTRIBUTES: Final = (
    "backend",
    "_lock_backend",
    "_rwlock_backend",
    "_election_backend",
    "_schedule_backend",
)
"""Where a Component keeps a bound backend.

Most keep one on `backend`. `Coordination` holds four, any of which may come
from a different Provider, so each is checked on its own.
"""

_SCOPE_RANK: Final[dict[str, int]] = {
    scope: rank for rank, scope in enumerate(get_args(BackendScope.__value__))
}
"""Scope by how far it shares, so `>=` answers whether a requirement is met."""

_UNKNOWN_ENVIRONMENT_MESSAGE: Final = (
    "%s=%r is not one of %s, so the backend check runs as if it were "
    "undeclared."
)
"""Report text, shared by the `warnings` and the `logging` channel."""

_UNDECLARED_MESSAGE: Final = (
    "%s is bound to %s, which %s scope %r, but requires scope %r.%s Set %s to "
    "declare where this runs, or pass requires=%r to say that is the reach "
    "you want."
)
"""Report text for an unmet requirement with no tier declared.

Names both sides of the match: a backend provides a scope, a component
requires one.
"""

_reported_unknown: set[str] = set()
"""Values already reported, so a second read stays quiet."""


@dataclass(frozen=True)
class Unmet:
    """One component whose backends do not reach as far as it requires.

    Backends of the same scope under one component are held together, so a
    `Coordination` whose four backends all come from one memory provider is
    reported once.
    """

    component: str
    """Label of the component holding them, `Coordination('default')`."""

    backends: tuple[str, ...]
    """Class names of the bound backends that fall short."""

    scope: BackendScope
    """How far those backends share what they hold."""

    requires: BackendScope
    """How far the component needs it shared."""

    @property
    def backend(self) -> str:
        """The backend names, read as a list."""
        if len(self.backends) == 1:
            return self.backends[0]
        return f"{', '.join(self.backends[:-1])} and {self.backends[-1]}"

    @property
    def provides(self) -> str:
        """`provides` or `provide`, agreeing with how many are named."""
        return "provides" if len(self.backends) == 1 else "provide"


def resolve_environment(explicit: Environment | None) -> Environment | None:
    """Return the declared tier, or `None` when nothing declares one.

    An explicit argument wins over `GREL_ENVIRONMENT`. A value outside the
    four tiers is reported once and read as undeclared.
    """
    if explicit is not None:
        return explicit
    value = os.environ.get(ENVIRONMENT_VAR, "").strip()
    if not value:
        return None
    for environment in ENVIRONMENTS:
        if value == environment:
            return environment
    _report_unknown_environment(value)
    return None


def _report_unknown_environment(value: str) -> None:
    """Report a value that names no tier, on both channels, once."""
    if value in _reported_unknown:
        return
    _reported_unknown.add(value)
    known = ", ".join(ENVIRONMENTS)
    message = _UNKNOWN_ENVIRONMENT_MESSAGE % (ENVIRONMENT_VAR, value, known)
    warnings.warn(message, GrelmicroConfigWarning, stacklevel=4)
    defer_report(
        partial(logger.warning, message, extra={"variable": ENVIRONMENT_VAR})
    )


def scope_of(backend: object) -> BackendScope | None:
    """Return how far `backend` shares state, or `None` when it says nothing.

    An Adapter that declares no `scope` is never reported.
    """
    scope = getattr(backend, "scope", None)
    return scope if scope in _SCOPE_RANK else None


def unmet_requirements(items: Iterable[object]) -> list[Unmet]:
    """Return every bound backend that falls short of its component.

    Only a bound backend is checked. A component that holds none, or that
    declares no requirement, is passed over.
    """
    grouped: dict[tuple[str, BackendScope, BackendScope], list[str]] = {}
    for item in items:
        requires = getattr(item, "requires", None)
        if requires not in _SCOPE_RANK:
            continue
        for attribute in BACKEND_ATTRIBUTES:
            backend = getattr(item, attribute, None)
            scope = scope_of(backend) if backend is not None else None
            if scope is None or _SCOPE_RANK[scope] >= _SCOPE_RANK[requires]:
                continue
            names = grouped.setdefault((label(item), scope, requires), [])
            name = type(backend).__name__
            if name not in names:
                names.append(name)
    return [
        Unmet(
            component=component,
            backends=tuple(names),
            scope=scope,
            requires=requires,
        )
        for (component, scope, requires), names in grouped.items()
    ]


def label(item: object) -> str:
    """Return `Coordination('default')` for a component, its class otherwise."""
    name = getattr(item, "name", None)
    if isinstance(name, str):
        return f"{type(item).__name__}({name!r})"
    return type(item).__name__


def report_unmet_requirements(
    unmet: Sequence[Unmet],
    environment: Environment | None,
) -> None:
    """Raise in a strict tier, warn when no tier is declared, else stay quiet.

    Raises:
        BackendScopeError: If the tier is `staging` or `production`.
    """
    if not unmet or environment in QUIET_ENVIRONMENTS:
        return
    if environment in STRICT_ENVIRONMENTS:
        raise BackendScopeError(strict_message(unmet, environment))
    entry = unmet[0]
    message = _UNDECLARED_MESSAGE % (
        entry.component,
        entry.backend,
        entry.provides,
        entry.scope,
        entry.requires,
        _others(len(unmet)),
        ENVIRONMENT_VAR,
        entry.scope,
    )
    warnings.warn(message, GrelmicroConfigWarning, stacklevel=4)
    # Rendered before it reaches `logging`, so both channels carry the same
    # sentence and the record holds no positional arguments a formatter
    # could read as something else.
    defer_report(
        partial(
            logger.warning,
            message,
            extra={
                "component": entry.component,
                "backend_scope": entry.scope,
                "requires": entry.requires,
            },
        )
    )


def _others(count: int) -> str:
    """Return the clause counting the findings the message does not name."""
    if count < 2:  # noqa: PLR2004
        return ""
    if count == 2:  # noqa: PLR2004
        return " One other binding does not hold either."
    return f" {count - 1} other bindings do not hold either."


def strict_message(
    unmet: Sequence[Unmet], environment: Environment | str
) -> str:
    """Render the error a strict tier raises, one sentence per finding."""
    lines = [
        f"{entry.component} is bound to {entry.backend}, which "
        f"{entry.provides} scope {entry.scope!r}, but requires scope "
        f"{entry.requires!r} in environment {environment!r}."
        for entry in unmet
    ]
    backends = (
        "a SQLite, Redis, Valkey, Postgres, or Kubernetes backend"
        if all(entry.requires == "host" for entry in unmet)
        else "a Redis, Valkey, Postgres, or Kubernetes backend"
    )
    lines.append(
        f"Use {backends}, or pass requires= to say what reach you want."
    )
    return " ".join(lines)
