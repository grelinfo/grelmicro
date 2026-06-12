"""Freeze guard for the public API surface.

The 1.0 promise is that the public API is frozen, so any change to an exported
symbol or its call signature must be deliberate and reviewed. This test
snapshots, for every public module, each exported symbol together with the
signature of its constructor (or call) and its public factory classmethods, and
fails when the live surface drifts from the snapshot.

When the surface changes on purpose, regenerate the snapshot with::

    pytest tests/test_public_api.py --snapshot-update

and review the ``__snapshots__`` diff as part of the change.

Signatures are captured with annotations stripped and defaults normalized to
``...``, keeping only the stable shape: parameter names, kinds (the ``*`` and
``/`` markers), and whether each has a default. Annotation reprs and default
values vary across Python and Pydantic versions and would make the guard flaky
across the supported matrix, so they are deliberately excluded. This catches a
parameter rename, removal, reorder, or required-to-optional flip, but not a
default-value change (that one is left to review and the changelog). A new
public module is itself a deliberate API change, so add it to
``PUBLIC_MODULES`` in the same change that introduces it.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import pytest
from syrupy.extensions.json import JSONSnapshotExtension

PUBLIC_MODULES = [
    "grelmicro",
    "grelmicro.cache",
    "grelmicro.clock",
    "grelmicro.config",
    "grelmicro.coordination",
    "grelmicro.fastapi",
    "grelmicro.health",
    "grelmicro.idempotency",
    "grelmicro.log",
    "grelmicro.metrics",
    "grelmicro.providers",
    "grelmicro.resilience",
    "grelmicro.resilience.backoffs",
    "grelmicro.resilience.circuitbreaker",
    "grelmicro.resilience.ratelimiter",
    "grelmicro.resilience.shield",
    "grelmicro.task",
    "grelmicro.testing",
    "grelmicro.trace",
]


@pytest.fixture
def snapshot_json(snapshot):  # noqa: ANN001, ANN201
    """Snapshot stored as a reviewable JSON file under ``__snapshots__``."""
    return snapshot.use_extension(JSONSnapshotExtension)


class _Default:
    """Renders a parameter's default as ``...`` regardless of its real value."""

    def __repr__(self) -> str:
        """Return the placeholder marker."""
        return "..."


_DEFAULT = _Default()


def _signature(obj: object) -> str | None:
    """Return a version-stable signature string, or None when not callable.

    Annotations are stripped and every default is normalized to ``...`` so the
    string carries only parameter names, kinds, and whether each has a default.
    """
    try:
        sig = inspect.signature(obj)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        return None
    params = [
        param.replace(
            annotation=inspect.Parameter.empty,
            default=(
                _DEFAULT
                if param.default is not inspect.Parameter.empty
                else inspect.Parameter.empty
            ),
        )
        for param in sig.parameters.values()
    ]
    return str(
        sig.replace(
            parameters=params, return_annotation=inspect.Signature.empty
        )
    )


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


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_every_exported_symbol_resolves(module_name: str) -> None:
    """Every name in a module's ``__all__`` resolves to a real attribute."""
    module = importlib.import_module(module_name)
    for name in module.__all__:
        assert getattr(module, name, None) is not None, (
            f"{module_name}.{name} is listed in __all__ but does not resolve"
        )
