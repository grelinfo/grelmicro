"""Freeze guard for the public API surface.

The 1.0 promise is that the public API is frozen, so any change to an exported
symbol or its call signature must be deliberate and reviewed. This test
snapshots, for every public module, each exported symbol together with the
signature of its constructor (or call) and its public factory classmethods, and
fails when the live surface drifts from the snapshot.

When the surface changes on purpose, regenerate the snapshot with::

    pytest tests/test_public_api.py --snapshot-update

and review the ``__snapshots__`` diff as part of the change.

Signatures are captured with parameter annotations stripped and defaults kept,
so the string carries parameter names, kinds (the ``*`` and ``/`` markers), and
the default values themselves. Parameter annotation reprs carry
``Annotated[..., Doc(...)]`` payloads that vary across Python and Pydantic
versions and would make the guard flaky, so those are excluded. Return
annotations are kept: they are part of the contract, they were measured
stable across the matrix, and no other tool guards them.

Defaults are kept for the same reason. Every default on the public surface
reprs identically on 3.12, 3.13 and 3.14, and only a sentinel's memory address
is normalised. So this catches a parameter rename, removal, reorder,
required-to-optional flip, a changed return type, and a changed default, which
is a behavioural break for every caller who never passed the argument.

A ``default_factory`` is resolved and recorded too, so changing what the
factory returns fails the guard: ``RetryConfig``'s backoff strategy is chosen
entirely by one. The factory is called twice and recorded only when both calls
agree, so a ``worker`` field minting a fresh identifier stays ``<factory>``
rather than writing a random value here. The result is normalised the same
way a literal default is, since a factory may hand back a shared object.

A default taken from an interpreter constant records the resolved value, so a
future CPython raising ``pickle.HIGHEST_PROTOCOL`` fails this snapshot. That
is a real change in what ``PickleSerializer()`` does, and
``test_interpreter_derived_defaults_are_still_current`` says which constant
moved so the update is informed rather than a mystery.

A new public module is itself a deliberate API change, so add it to
``PUBLIC_MODULES`` in the same change that introduces it.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from syrupy.extensions.json import JSONSnapshotExtension

if TYPE_CHECKING:
    from collections.abc import Callable

PUBLIC_MODULES = [
    "grelmicro",
    "grelmicro.cache",
    "grelmicro.clock",
    "grelmicro.config",
    "grelmicro.coordination",
    "grelmicro.describe",
    "grelmicro.health",
    "grelmicro.http",
    "grelmicro.idempotency",
    "grelmicro.integrations",
    "grelmicro.integrations.fastapi",
    "grelmicro.integrations.faststream",
    "grelmicro.integrations.litestar",
    "grelmicro.integrations.starlette",
    "grelmicro.log",
    "grelmicro.metrics",
    "grelmicro.outbox",
    "grelmicro.providers",
    "grelmicro.resilience",
    "grelmicro.resilience.backoffs",
    "grelmicro.resilience.circuitbreaker",
    "grelmicro.resilience.ratelimiter",
    "grelmicro.resilience.shield",
    "grelmicro.security",
    "grelmicro.task",
    "grelmicro.testing",
    "grelmicro.trace",
    "grelmicro.types",
]
"""Every module the API reference renders, which is what public means here.

A module `docs/reference/` documents is one a reader is told to import
from, so its `__all__` is a promise and belongs in the snapshot.
`grelmicro.outbox` and `grelmicro.types` were documented but unguarded,
so their exports could change without the snapshot noticing.
"""


@pytest.fixture
def snapshot_json(snapshot):  # noqa: ANN001, ANN201
    """Snapshot stored as a reviewable JSON file under ``__snapshots__``."""
    return snapshot.use_extension(JSONSnapshotExtension)


_MODULE_PATH = re.compile(r"\b[a-zA-Z_][\w.]*\.(?=[A-Za-z_]\w*)")
"""Matches the module qualifier in front of a rendered type name.

A return type is recorded by name, not by where the name lives. Keeping the
path would fail the guard when a private class is renamed, or when a public
one moves behind its re-export, neither of which changes the public surface.
"""

_ADDRESS = re.compile(r" at 0x[0-9a-f]+")
"""Matches the identity part of a default `object.__repr__`.

The only default in the surface whose repr carries one is the `_UNSET`
sentinel shared by the `Fallback` doors. Everything else reprs stably.
"""


class _Rendered:
    """Renders a default as a fixed string, whatever its real repr is."""

    def __init__(self, text: str) -> None:
        """Hold the already-normalized text."""
        self._text = text

    def __repr__(self) -> str:
        """Return the normalized default."""
        return self._text


def _default(value: object) -> _Rendered:
    """Return a matrix-stable rendering of a parameter default.

    Defaults are recorded, not blanked. A shipped default is part of the
    contract: `timeout=30` becoming `timeout=5` changes behaviour for every
    caller who never passed the argument, and blanking it made that
    invisible here.

    Measured before trusting it: all 392 defaults on the public surface
    repr identically on 3.12, 3.13 and 3.14. Only a memory address varies,
    so only that is normalized.
    """
    return _Rendered(_ADDRESS.sub(" at 0x...", repr(value)))


def _factory_default(owner: object, name: str) -> str | None:
    """Return a pydantic ``default_factory`` result, when it is stable.

    The signature only carries pydantic's opaque ``<factory>`` sentinel, so
    the factory is looked up on the model and called. Changing what a
    factory returns is a behavioural break exactly like changing a literal
    default: `RetryConfig`'s backoff strategy is chosen entirely by one.

    Called twice and recorded only when both calls agree. A `worker` field
    mints a fresh identifier every time, so it stays `<factory>` rather
    than writing a random value into the snapshot.
    """
    fields = getattr(owner, "model_fields", None)
    if not isinstance(fields, dict):
        return None
    field = fields.get(name)
    if field is None:
        # The signature shows an aliased field under its alias, so a
        # name-only lookup would miss it and leave it as `<factory>`, which
        # is the blind spot this exists to close.
        field = next(
            (
                candidate
                for candidate in fields.values()
                if getattr(candidate, "alias", None) == name
            ),
            None,
        )
    factory = getattr(field, "default_factory", None)
    if factory is None:
        return None
    try:
        first, second = factory(), factory()
    except Exception:  # noqa: BLE001  # pragma: no cover  # needs arguments
        return None
    if repr(first) != repr(second):
        return None
    # Normalised like a literal default. A factory returning a shared object
    # reprs identically on both calls, so it passes the determinism check and
    # would otherwise write a memory address into the committed snapshot.
    return _ADDRESS.sub(" at 0x...", repr(first))


def _signature(obj: object) -> str | None:
    """Return a version-stable signature string, or None when not callable.

    Parameter annotations are stripped, since those do vary across the
    supported Python and Pydantic versions. Defaults and the return
    annotation are kept, because they do not: the string carries parameter
    names, kinds, the default values, and the return type.
    """
    try:
        sig = inspect.signature(obj)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        return None
    params = [
        param.replace(
            annotation=inspect.Parameter.empty,
            default=(
                _Rendered(factory)
                if (factory := _factory_default(obj, param.name)) is not None
                else _default(param.default)
                if param.default is not inspect.Parameter.empty
                else inspect.Parameter.empty
            ),
        )
        for param in sig.parameters.values()
    ]
    return str(sig.replace(parameters=params, return_annotation=_returns(sig)))


def _returns(sig: inspect.Signature) -> object:
    """Return the annotation to record for a signature's return type.

    Kept, unlike parameter annotations. A return type is part of the
    contract: ``-> tzinfo`` becoming ``-> str`` breaks every caller, and
    nothing else guards it. Griffe does not either, despite its docs: its
    ``_returns_are_compatible`` ends in a ``TODO`` and returns ``True``.

    Measured before trusting it, the same way defaults were: all 145 return
    annotations on the public surface render identically on 3.12, 3.13 and
    3.14. Parameter annotations are a different matter, since those carry
    ``Annotated[..., Doc(...)]`` payloads, and stay stripped.
    """
    annotation = sig.return_annotation
    if annotation is inspect.Signature.empty:
        return inspect.Signature.empty
    # `formatannotation`, not `str`. `str` on an evaluated class gives
    # `<class 'grelmicro.cache.cached._CachedDecorator'>`, which writes a
    # private module path into the public snapshot, and renders `bool` and
    # `Self` differently depending on whether the defining module uses PEP
    # 563. Both would fail the guard on a rename that changes nothing public.
    rendered = inspect.formatannotation(annotation)
    return _Rendered(_MODULE_PATH.sub("", _ADDRESS.sub(" at 0x...", rendered)))


def _symbol_surface(obj: object) -> dict[str, str] | None:
    """Return ``{"()": ctor_sig, ".factory": sig, ...}`` for one exported symbol.

    Captures the constructor or call signature plus every public classmethod and
    staticmethod the class defines itself (its factory surface). Returns None for
    symbols with no introspectable signature (type aliases, some protocols).
    """
    surface: dict[str, str] = {}
    call_sig = _signature(obj)
    if call_sig is not None:
        surface["()"] = call_sig
    if isinstance(obj, type):
        for attr_name, attr in vars(obj).items():
            if attr_name.startswith("_"):
                continue
            if isinstance(attr, (classmethod, staticmethod)):
                method_sig = _signature(getattr(obj, attr_name))
                if method_sig is not None:
                    surface[f".{attr_name}"] = method_sig
    return surface or None


def build_public_api() -> dict[str, dict[str, Any]]:
    """Return ``{module: {symbol: signature_surface}}`` for every public module."""
    surface: dict[str, dict[str, Any]] = {}
    for name in PUBLIC_MODULES:
        module = importlib.import_module(name)
        exported = getattr(module, "__all__", None)
        if exported is None:
            msg = f"{name} has no __all__, so its public surface is undeclared"
            raise AssertionError(msg)
        surface[name] = {
            symbol: _symbol_surface(getattr(module, symbol))
            for symbol in sorted(exported)
        }
    return surface


def test_public_api_matches_snapshot(snapshot_json) -> None:  # noqa: ANN001
    """The live public surface equals the committed snapshot."""
    assert build_public_api() == snapshot_json


_LOOP_BOUND = re.compile(
    r"asyncio\.(?:Lock|Event|Condition|Semaphore|BoundedSemaphore|Barrier"
    r"|Queue|LifoQueue|PriorityQueue|Future|AbstractEventLoop"
    r"|AbstractEventLoopPolicy)\b"
    r"|\bAbstractEventLoop\b"
)
"""Objects that bind to the event loop that first awaits them."""

_LOOP_BOUND_ALLOWED = {
    "grelmicro.providers.sqlite.SQLiteProvider.connection_lock",
}
"""Members that hand out a loop-bound object on purpose.

An adapter serializes its `execute` and its `fetch` against every other
adapter borrowing the same connection, so the lock belongs to the provider
and an adapter author has to reach it.
"""


def _public_members(obj: object) -> list[tuple[str, Callable[..., Any]]]:
    """Return every public callable a class defines itself, plus the class.

    A property is represented by its getter, which carries the return
    annotation the guard reads.
    """
    members: list[tuple[str, Callable[..., Any]]] = []
    if callable(obj):
        members.append(("()", obj))
    if isinstance(obj, type):
        for attr_name, attr in vars(obj).items():
            if attr_name.startswith("_"):
                continue
            target = attr.fget if isinstance(attr, property) else attr
            if callable(target):
                members.append((f".{attr_name}", target))
    return members


def test_public_api_hands_out_no_loop_bound_object() -> None:
    """No public signature accepts or returns an object bound to an event loop.

    Supporting several event loops means making the primitives a component
    holds per loop. That stays an internal change for as long as none of
    them reaches the public surface, so this guard is what keeps
    [#693](https://github.com/grelinfo/grelmicro/issues/693) from turning
    into a breaking change. Keep the primitives on private attributes.

    `SQLiteProvider.connection_lock` is the deliberate exception, and it is
    not a counterexample. An adapter has to serialize its `execute` and its
    `fetch` against every other adapter borrowing the same connection, so
    the lock belongs to the provider and an adapter author needs to reach
    it. A per-loop lock would be the wrong answer there anyway: aiosqlite
    drives one connection from one worker thread and resolves each call on
    the loop that made it, so a provider already belongs to a single loop.
    """
    offenders: list[str] = []
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for symbol in sorted(getattr(module, "__all__", ())):
            obj = getattr(module, symbol)
            for label, member in _public_members(obj):
                qualified = f"{module_name}.{symbol}{label}"
                if qualified in _LOOP_BOUND_ALLOWED:
                    continue
                try:
                    rendered = str(inspect.signature(member))
                except (TypeError, ValueError):
                    continue
                if _LOOP_BOUND.search(rendered):
                    offenders.append(f"{qualified}: {rendered}")
    assert not offenders, (
        "loop-bound objects on the public surface:\n" + "\n".join(offenders)
    )


def test_loop_bound_allowlist_still_describes_something_real() -> None:
    """Every allowlisted member exists and still hands out a loop-bound object.

    An exemption that stopped applying is one a later change inherits by
    accident, so it has to be earned on every run.
    """
    for qualified in sorted(_LOOP_BOUND_ALLOWED):
        module_name, symbol, attr = qualified.rsplit(".", 2)
        module = importlib.import_module(module_name)
        member = getattr(type(getattr(module, symbol)), attr, None) or getattr(
            getattr(module, symbol), attr
        )
        target = member.fget if isinstance(member, property) else member
        rendered = str(inspect.signature(target))
        assert _LOOP_BOUND.search(rendered), (
            f"{qualified} no longer hands out a loop-bound object "
            f"({rendered}), so drop it from the allowlist"
        )


def test_interpreter_derived_defaults_are_still_current() -> None:
    """Name the defaults the snapshot records from an interpreter constant.

    The snapshot stores the resolved value, so raising the constant fails
    it. That failure is correct, since the effective default really did
    change, but without this test the reviewer sees `protocol=5` become
    `protocol=6` with nothing saying why.
    """
    import pickle  # noqa: PLC0415

    from grelmicro.cache.serializers import PickleSerializer  # noqa: PLC0415

    default = inspect.signature(PickleSerializer).parameters["protocol"]
    assert default.default == pickle.HIGHEST_PROTOCOL, (
        "PickleSerializer no longer defaults to pickle.HIGHEST_PROTOCOL, so "
        "the snapshot entry for it is recording something else"
    )


def test_every_documented_module_is_snapshotted() -> None:
    """A module the reference renders cannot slip out of the snapshot.

    `grelmicro.outbox` and `grelmicro.types` were documented for
    releases while their exports went unguarded. Reading the reference
    pages closes that by construction, so adding a page adds the guard.
    """
    reference = Path(__file__).parent.parent / "docs" / "reference"
    documented = {
        match.group(1)
        for page in reference.glob("*.md")
        for match in re.finditer(
            r"^::: (grelmicro[\w.]*)$",
            page.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }
    guarded = set(PUBLIC_MODULES)
    missing = sorted(
        name
        for name in documented - guarded
        if hasattr(importlib.import_module(name), "__all__")
    )
    assert not missing, (
        f"documented in docs/reference/ but absent from PUBLIC_MODULES: {missing}"
    )


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_every_exported_symbol_resolves(module_name: str) -> None:
    """Every name in a module's ``__all__`` resolves to a real attribute."""
    module = importlib.import_module(module_name)
    for name in module.__all__:
        assert getattr(module, name, None) is not None, (
            f"{module_name}.{name} is listed in __all__ but does not resolve"
        )


# Adapter families that must stay symmetric on the top-level package. Each
# tuple is ``(package, base_name, [backend, ...])``. Every backend the package
# ships must export a ``{Backend}{base_name}`` symbol from ``__all__``, so a
# documented adapter can never silently drop off the public surface (as the
# SQLite circuit breaker adapter once did).
_ADAPTER_FAMILIES = [
    (
        "grelmicro.resilience",
        "CircuitBreakerAdapter",
        ["Memory", "Redis", "Postgres", "SQLite"],
    ),
    (
        "grelmicro.resilience",
        "RateLimiterAdapter",
        ["Memory", "Redis", "Postgres", "SQLite"],
    ),
]


@pytest.mark.parametrize(("package", "base", "backends"), _ADAPTER_FAMILIES)
def test_adapter_family_export_parity(
    package: str, base: str, backends: list[str]
) -> None:
    """Every backend in an adapter family is exported from the package."""
    module = importlib.import_module(package)
    exported = set(module.__all__)
    for backend in backends:
        symbol = f"{backend}{base}"
        assert symbol in exported, (
            f"{package} exports other {base} backends but is missing "
            f"{symbol}. Add it to __all__ and the lazy import map."
        )
        assert getattr(module, symbol, None) is not None, (
            f"{package}.{symbol} is listed in __all__ but does not resolve"
        )
