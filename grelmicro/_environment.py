"""Deployment environment and the backend scope check.

Exposes:

- `resolve_environment`: read the declared tier from an argument or
  `GREL_ENVIRONMENT`.
- `unmet_requirements`: walk registered items and recorded bindings, and
  return every backend whose scope falls short of what is required of it.
- `report_unmet_requirements`: raise or warn, by declared tier.
- `record`, `record_coordination`, `forget`, `recorded_bindings`: keep the
  bindings of patterns no app registers, for the next app to check.

The user-facing rules are in `docs/deployment.md`, the model in
`docs/architecture/backends.md`.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections import Counter
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from itertools import chain
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Final, NamedTuple, cast, get_args
from weakref import WeakKeyDictionary, WeakSet

from grelmicro._config import defer_report
from grelmicro._diagnostics import (
    BACKEND_SCOPE,
    UNKNOWN_ENVIRONMENT,
    diagnostic,
)
from grelmicro.errors import (
    BackendScopeError,
    BackendScopeWarning,
    SettingsValidationError,
    UnknownEnvironmentWarning,
)
from grelmicro.types import BackendScope, Environment

if TYPE_CHECKING:
    from collections.abc import (
        Generator,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )

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


def backend_attributes() -> tuple[str, ...]:
    """Return the attributes a Component keeps a bound backend on.

    Most keep one on `backend`. `Coordination` keeps one per entry in
    `COORDINATION_BACKENDS`, any of which may come from a different Provider,
    so each is checked on its own.
    """
    from grelmicro.coordination._component import (  # noqa: PLC0415
        COORDINATION_BACKENDS,
    )

    return (
        "backend",
        *(f"_{slot.keyword}_backend" for slot in COORDINATION_BACKENDS),
    )


_SCOPE_RANK: Final[dict[str, int]] = {
    scope: rank for rank, scope in enumerate(get_args(BackendScope.__value__))
}
"""Scope by how far it shares, so `>=` answers whether a requirement is met."""

_UNKNOWN_ENVIRONMENT_MESSAGE: Final = (
    "%s is set to a value that is not one of %s, so the backend check runs "
    "as if it were undeclared."
)
"""Report text, shared by the `warnings` and the `logging` channel."""

_reported_unknown: set[str] = set()
"""Values already reported, so a second read stays quiet."""

_PACKAGE_DIR: Final = f"{Path(__file__).parent}{os.sep}"
"""Frames under this directory are skipped, so a warning names user code."""

_reported_constructions: set[tuple[object, ...]] = set()
"""The shapes of findings already warned about at construction.

With no tier declared, a pattern built per request would otherwise warn on
every request. Keyed by class and backend, not by name, so a lock named per
request stays one entry. A strict tier still raises every time.
"""


@dataclass(frozen=True)
class Binding:
    """The backend something holds, or the component whose backend it rides.

    A pattern that no app registers is kept as one of these until an app
    checks it. A component whose backend lives on another component
    describes itself through `_scope_bindings()`.
    """

    label: str
    """How the report names it, `Lock('cart')`."""

    requires: BackendScope
    """How far it needs what the backend holds shared."""

    backend: object | None = None
    """The backend it holds, when it holds one of its own."""

    rides: tuple[str, str] | None = None
    """`(kind, name)` of the component whose backend it reads otherwise."""

    attribute: str = "backend"
    """The attribute of the ridden component that holds the backend."""

    absent: object | None = None
    """What it falls back to when the ridden component holds no backend.

    `None` leaves such a binding unchecked.
    """

    slot: str | None = None
    """The `Coordination` keyword that takes this backend.

    Set for a coordination pattern, which has no `requires=` of its own. A
    registered component holding the same backend decides for it, and the
    report points at `Coordination(slot=..., requires=...)` as the way out.
    """

    kind: str | None = None
    """Component kind of the backend, `"coordination"` or `"cache"`."""

    keyword: str = "requires"
    """The argument that accepts a smaller reach, `lock` on `@cached`."""


_recorded: WeakKeyDictionary[object, Binding] = WeakKeyDictionary()
"""The binding of every live pattern no app registers, by pattern.

Held weakly, so a pattern that is gone is no longer checked.
"""

_recorded_lock = Lock()
"""Guards `_recorded`, which a pattern built on any thread writes to."""

_answered: WeakSet[object] = WeakSet()
"""Backends a registered component holds, and so answers for.

A pattern that `micro.coordination.lock(...)` builds holds the component's
backend, so it is left to the component, and building one stays one lookup.
"""

SUSPENDED: ContextVar[bool] = ContextVar("grelmicro_unrecorded", default=False)
"""Set while a component builds a pattern it answers for itself."""


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

    rides: str | None = None
    """Label of the component whose backend it reads, `Cache('default')`."""

    slot: str | None = None
    """The `Coordination` keyword to register the backend under, if any."""

    kind: str | None = None
    """Component kind of the backends, which decides the backends offered."""

    keyword: str = "requires"
    """The argument that accepts a smaller reach, `lock` on `@cached`."""

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

    @property
    def finding(self) -> str:
        """The sentence naming what falls short, with no tier and no stop."""
        if self.rides is not None:
            return (
                f"{self.component} rides {self.rides}, which is bound to "
                f"{self.backend} and {self.provides} scope {self.scope!r}, "
                f"but requires scope {self.requires!r}"
            )
        return (
            f"{self.component} is bound to {self.backend}, which "
            f"{self.provides} scope {self.scope!r}, but requires scope "
            f"{self.requires!r}"
        )

    def remedy(self, scope: str | None = None) -> str:
        """Return how to accept a smaller reach, naming `scope` when given."""
        if self.slot is None:
            return f"pass {self.keyword}={scope or ''}"
        return (
            f"register it as Coordination({self.slot}=..., "
            f"requires={scope or '...'})"
        )


def resolve_environment(explicit: Environment | None) -> Environment | None:
    """Return the declared tier, or `None` when nothing declares one.

    An explicit argument wins over `GREL_ENVIRONMENT`. The two doors treat
    an unknown tier differently on purpose. The argument is code, so it
    raises. The variable is the operator's, so it is reported once and read
    as undeclared.

    Raises:
        SettingsValidationError: If `explicit` names no known tier.
    """
    if explicit is not None:
        if explicit not in ENVIRONMENTS:
            known = ", ".join(ENVIRONMENTS)
            msg = f"environment= must be one of {known}"
            raise SettingsValidationError(msg)
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
    message = diagnostic(
        UNKNOWN_ENVIRONMENT,
        _UNKNOWN_ENVIRONMENT_MESSAGE % (ENVIRONMENT_VAR, known),
    )
    warnings.warn(message, UnknownEnvironmentWarning, stacklevel=4)
    defer_report(
        partial(
            logger.warning,
            message,
            extra={
                "variable": ENVIRONMENT_VAR,
                "diagnostic": UNKNOWN_ENVIRONMENT,
            },
        )
    )


def scope_of(backend: object) -> BackendScope | None:
    """Return how far `backend` shares state, or `None` when it says nothing.

    An Adapter that declares no `scope` is never reported.
    """
    scope = getattr(backend, "scope", None)
    return scope if scope in _SCOPE_RANK else None


def record(pattern: object, binding: Binding) -> None:
    """Keep `binding` for `pattern` until an app checks it.

    The next app to open checks it, and so does `check_backends`. Built
    inside an open app, it is checked against that app at once.

    Raises:
        BackendScopeError: If an app is open, its tier is `staging` or
            `production`, and the backend falls short.
    """
    from grelmicro._app import _current_micro  # noqa: PLC0415

    if SUSPENDED.get():
        return
    with _recorded_lock:
        _recorded[pattern] = binding
    micro = _current_micro.get(None)
    if micro is None or micro.environment in QUIET_ENVIRONMENTS:
        return
    unmet = micro._unmet_bindings([binding])  # noqa: SLF001
    if not unmet:
        return
    if micro.environment not in STRICT_ENVIRONMENTS:
        shape = (
            type(pattern),
            *((entry.backends, entry.rides, entry.slot) for entry in unmet),
        )
        with _recorded_lock:
            if shape in _reported_constructions:
                return
            _reported_constructions.add(shape)
    report_unmet_requirements(unmet, micro.environment)


def record_coordination(
    pattern: object,
    backend: object,
    slot: str,
    requires: BackendScope | None = None,
) -> None:
    """Record a coordination pattern holding a backend of its own.

    Only a backend that falls short of `requires` is recorded, so a pattern
    on Redis costs one comparison. `requires` defaults to what
    `Coordination` requires, and a `Coordination` building the pattern
    passes its own.

    Raises:
        BackendScopeError: As `record` does.
    """
    if requires is None:
        from grelmicro.coordination._component import (  # noqa: PLC0415
            Coordination,
        )

        requires = Coordination.default_requires
    if falls_short(backend, requires) is None or _is_answered(backend):
        return
    record(
        pattern,
        Binding(
            label(pattern),
            requires,
            backend=backend,
            slot=slot,
            kind="coordination",
        ),
    )


def answer_for(component: object) -> None:
    """Mark every backend a registered `component` holds as answered for."""
    for backend in _answering_backends(component):
        with suppress(TypeError):
            _answered.add(backend)


@contextmanager
def unrecorded() -> Generator[None]:
    """Build patterns that are not recorded, because the caller answers."""
    token = SUSPENDED.set(True)
    try:
        yield
    finally:
        SUSPENDED.reset(token)


def _is_answered(backend: object) -> bool:
    """Return whether a `Coordination` holds `backend`."""
    try:
        return backend in _answered
    except TypeError:
        return False


def recorded_bindings() -> list[Binding]:
    """Return the binding of every live pattern no app registers."""
    with _recorded_lock:
        return list(_recorded.values())


def unmet_requirements(
    items: Iterable[object],
    bindings: Iterable[Binding] = (),
    *,
    check_items: bool = True,
    components: Mapping[tuple[str, str], object] | None = None,
) -> list[Unmet]:
    """Return every bound backend that falls short of what it must hold.

    Only a bound backend is checked. A component that holds none, or that
    declares no requirement, is passed over. `bindings` are checked against
    `items`: a binding that rides a component reads that component's
    backend, and a coordination pattern whose backend a registered component
    also holds is left to that component. `check_items=False` checks the
    bindings alone. `components` is the app's resolution index, which a
    binding riding `(kind, name)` reads. Without it, the index is built from
    `items` by the same rule.
    """
    items = list(items)
    if components is None:
        components = with_sole_defaults(
            {
                (kind, name): item
                for item in items
                if isinstance(kind := getattr(item, "kind", None), str)
                and isinstance(name := getattr(item, "name", None), str)
            }
        )
    grouped: dict[_Finding, list[str]] = {}
    findings = chain(
        _item_findings(items, components) if check_items else (),
        _binding_findings(bindings, items, components),
    )
    for finding, backend in findings:
        names = grouped.setdefault(finding, [])
        name = type(backend).__name__
        if name not in names:
            names.append(name)
    return [
        Unmet(
            component=finding.component,
            backends=tuple(names),
            scope=finding.scope,
            requires=finding.requires,
            rides=finding.rides,
            slot=finding.slot,
            kind=finding.kind,
            keyword=finding.keyword,
        )
        for finding, names in grouped.items()
    ]


class _Finding(NamedTuple):
    """What one component or pattern holds short, less the backend names."""

    component: str
    rides: str | None
    scope: BackendScope
    requires: BackendScope
    slot: str | None
    kind: str | None
    keyword: str = "requires"


def falls_short(backend: object, requires: object) -> BackendScope | None:
    """Return the backend's scope when it reaches less far than `requires`.

    A requirement that names no scope is passed over, as on a component.
    """
    scope = scope_of(backend)
    needed = _SCOPE_RANK.get(requires) if isinstance(requires, str) else None
    if scope is None or needed is None or _SCOPE_RANK[scope] >= needed:
        return None
    return scope


def _held_backends(item: object) -> list[object]:
    """Return the backends a registered item keeps bound."""
    return [
        backend
        for attribute in backend_attributes()
        if (backend := getattr(item, attribute, None)) is not None
    ]


def _answering_backends(item: object) -> list[object]:
    """Return the backends `item` answers for, by declaring a requirement.

    A pattern registered as a plain context manager holds a backend but
    states no reach for it, so it answers for nothing, not even its own.
    """
    if getattr(item, "requires", None) not in _SCOPE_RANK:
        return []
    return _held_backends(item)


def _item_findings(
    items: Sequence[object],
    components: Mapping[tuple[str, str], object],
) -> Iterator[tuple[_Finding, object]]:
    """Yield what every registered item holds short of its requirement."""
    for item in items:
        describe = getattr(item, "_scope_bindings", None)
        if describe is not None:
            yield from _binding_findings(describe(), items, components)
            continue
        requires = getattr(item, "requires", None)
        kind = getattr(item, "kind", None)
        for backend in _held_backends(item):
            scope = falls_short(backend, requires)
            if scope is not None:
                finding = _Finding(
                    label(item),
                    None,
                    scope,
                    cast("BackendScope", requires),
                    None,
                    kind if isinstance(kind, str) else None,
                )
                yield finding, backend


def _binding_findings(
    bindings: Iterable[Binding],
    items: Sequence[object],
    components: Mapping[tuple[str, str], object],
) -> Iterator[tuple[_Finding, object]]:
    """Yield what each binding holds or rides short of its requirement."""
    held: set[int] | None = None
    for binding in bindings:
        ridden: object | None = None
        backend = binding.backend
        if backend is None:
            ridden = (
                None if binding.rides is None else components.get(binding.rides)
            )
            backend = getattr(ridden, binding.attribute, None)
            if backend is None:
                backend = binding.absent
                ridden = None
            if backend is None:
                continue
        elif binding.slot is not None:
            if held is None:
                held = {
                    id(backend)
                    for item in items
                    for backend in _answering_backends(item)
                }
            if id(backend) in held:
                continue
        scope = falls_short(backend, binding.requires)
        if scope is not None:
            finding = _Finding(
                binding.label,
                None if ridden is None else label(ridden),
                scope,
                binding.requires,
                binding.slot,
                binding.kind,
                binding.keyword,
            )
            yield finding, backend


def with_sole_defaults[T](
    by_key: Mapping[tuple[str, str], T],
) -> dict[tuple[str, str], T]:
    """Return `by_key` plus `(kind, "default")` for the sole entry of a kind.

    A kind with exactly one entry and none named `"default"` answers
    `"default"` with that entry. This is how `Grelmicro.get` resolves.
    """
    counts = Counter(kind for kind, _ in by_key)
    resolved = dict(by_key)
    for (kind, _), entry in by_key.items():
        if counts[kind] == 1:
            resolved.setdefault((kind, "default"), entry)
    return resolved


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
    message = diagnostic(
        BACKEND_SCOPE,
        f"{entry.finding}.{_others(len(unmet))} Set {ENVIRONMENT_VAR} to "
        f"declare where this runs, or {entry.remedy(repr(entry.scope))} to "
        "say that is the reach you want.",
    )
    warnings.warn(
        message, BackendScopeWarning, skip_file_prefixes=(_PACKAGE_DIR,)
    )
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
                "diagnostic": BACKEND_SCOPE,
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
        f"{entry.finding} in environment {environment!r}." for entry in unmet
    ]
    names = ["Redis", "Valkey", "Postgres"]
    if all(entry.requires == "host" for entry in unmet):
        names.insert(0, "SQLite")
    if all(entry.kind == "coordination" for entry in unmet):
        names.append("Kubernetes")
    backends = f"a {', '.join(names[:-1])}, or {names[-1]} backend"
    remedies = " or ".join(dict.fromkeys(entry.remedy() for entry in unmet))
    lines.append(f"Use {backends}, or {remedies} to say what reach you want.")
    return " ".join(lines)
