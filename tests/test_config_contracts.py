"""The environment contract, swept over every class that reads it.

[Configuration internals](../docs/architecture/config.md) states nine rules.
Each one holds for a family, and each was written down because it had already
been broken once: R4's prefix was renamed across twelve releases, and
`reconfigure` refused the config class its own docs told you to build, for
every pattern at once.

A per-class test cannot keep a family rule honest. These sweep the family.
"""

import ast
import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, ForwardRef, get_args

import pytest
from pydantic import BaseModel

import grelmicro
from grelmicro._config import Reconfigurable, env_prefixes
from grelmicro.cache import TTLCache

PACKAGE_ROOT = Path(grelmicro.__file__).parent

_MIN_RECONFIGURABLES = 12
"""Floor for the sweep, so an empty scan cannot pass silently."""


def _discover_reconfigurables() -> list[tuple[str, str]]:
    """Return `(module, class)` for every class declaring `Reconfigurable`."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = (
            "grelmicro."
            + path.relative_to(PACKAGE_ROOT).with_suffix("").as_posix()
        ).replace("/", ".")
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            for base in node.bases:
                target = base
                if isinstance(target, ast.Subscript):
                    target = target.value
                name = getattr(target, "id", None) or getattr(
                    target, "attr", None
                )
                if name == "Reconfigurable":
                    found.append((module, node.name))
                    break
    return found


RECONFIGURABLES = _discover_reconfigurables()

IDS = [
    f"{module}.{name}".rsplit(".", 2)[-1] for module, name in RECONFIGURABLES
]


def test_the_sweep_finds_reconfigurables() -> None:
    """An empty or shrunken scan is a failure, never a silent pass."""
    assert len(RECONFIGURABLES) >= _MIN_RECONFIGURABLES, (
        f"expected at least {_MIN_RECONFIGURABLES}, got {RECONFIGURABLES}"
    )


def _resolve(module_name: str, class_name: str) -> Any:  # noqa: ANN401
    """Return the class from its module."""
    return getattr(importlib.import_module(module_name), class_name)


def test_a_nameless_object_is_not_reconfigurable() -> None:
    """R3: no name means no address, so nothing to reload against.

    `TTLCache` is the example the rule is written around. It reads no
    variable and has no live reload precisely because it has no name, and
    the Settled table says to reopen the decision only if it gains one.
    """
    assert not issubclass(TTLCache, Reconfigurable), (
        "TTLCache became Reconfigurable without gaining a name, so the "
        "environment has no address for it (R3)"
    )
    assert "name" not in inspect.signature(TTLCache.__init__).parameters


def _declared_configs(cls: Any) -> list[Any]:  # noqa: ANN401
    """Return every config class declared on `Reconfigurable[...]`.

    A pattern with several algorithms declares a discriminated union, so
    there is more than one, and the alias is a forward reference served
    lazily by the module. Both are unwrapped here rather than skipped: a
    skip would quietly drop the two classes whose union grows most often.
    """
    module = sys.modules[cls.__module__]
    pending: list[Any] = []
    for base in getattr(cls, "__orig_bases__", ()):
        if getattr(base, "__origin__", None) is Reconfigurable:
            pending.extend(get_args(base))
    found: list[Any] = []
    while pending:
        candidate = pending.pop()
        if isinstance(candidate, ForwardRef):
            candidate = getattr(module, candidate.__forward_arg__, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            found.append(candidate)
            continue
        pending.extend(get_args(candidate) or ())
        pending.extend(getattr(candidate, "__metadata__", ()) or ())
    return found


@pytest.mark.parametrize(
    ("module_name", "class_name"), RECONFIGURABLES, ids=IDS
)
def test_reconfigure_declares_the_config_class_it_accepts(
    module_name: str, class_name: str
) -> None:
    """The class states which config `reconfigure` takes, in its base.

    An instance built through the environment holds a settings subclass, so
    a runtime check against that subclass rejected the plain config the
    documentation names. Every pattern was affected at once, which is what
    makes this a sweep rather than a unit test.
    """
    cls = _resolve(module_name, class_name)
    assert _declared_configs(cls), (
        f"{class_name} declares no config class on Reconfigurable[...], so "
        f"what reconfigure accepts is unstated"
    )


KIND_SEGMENTS = {
    "Lock": "LOCK",
    "TaskLock": "TASKLOCK",
    "LeaderElection": "LEADERELECTION",
    "ReadWriteLock": "READWRITELOCK",
    "Timeout": "TIMEOUT",
    "Retry": "RETRY",
    "Bulkhead": "BULKHEAD",
    "Fallback": "FALLBACK",
    "Shield": "SHIELD",
    "RateLimiter": "RATELIMITER",
    "CircuitBreaker": "CIRCUITBREAKER",
}
"""R4's published table: the class name uppercased, separators dropped.

Written out rather than derived, so a rename has to be noticed here and in
the docs together. Deriving it would make the test agree with whatever the
code does, which is the opposite of a contract.
"""


@pytest.mark.parametrize(("class_name", "segment"), KIND_SEGMENTS.items())
def test_default_instance_owns_the_bare_prefix(
    class_name: str, segment: str
) -> None:
    """R4: the derived prefix matches the table, and R3's default rule.

    The default instance drops the name segment, so it owns the kind
    address. A named instance keeps it and falls back to the kind address,
    which is the row that silently stopped working in 0.38.
    """
    instance, kind = env_prefixes(segment, "default")
    assert instance == f"GREL_{segment}_"
    assert kind is None, (
        f"{class_name} default instance should own the bare prefix, with "
        f"nothing left to fall back to"
    )

    named_instance, named_kind = env_prefixes(segment, "cart")
    assert named_instance == f"GREL_{segment}_CART_"
    assert named_kind == f"GREL_{segment}_", (
        f"a named {class_name} must fall back to the kind address, which is "
        f"how one variable retunes a whole kind"
    )


def test_every_reconfigurable_declares_its_config_type() -> None:
    """`Reconfigurable` is generic, so the config type is part of the API."""
    missing = []
    for module_name, class_name in RECONFIGURABLES:
        cls = _resolve(module_name, class_name)
        if not issubclass(cls, Reconfigurable):  # pragma: no cover
            missing.append(f"{class_name} does not subclass Reconfigurable")
            continue
        config = getattr(cls, "config", None)
        if config is None:
            missing.append(f"{class_name} exposes no config property")
    assert not missing, missing


@pytest.mark.parametrize(
    ("module_name", "class_name"), RECONFIGURABLES, ids=IDS
)
def test_the_config_is_a_frozen_pydantic_model(
    module_name: str, class_name: str
) -> None:
    """A live-reloaded config is swapped, never mutated in place.

    `reconfigure` publishes a new object so an in-flight call keeps the
    snapshot it started with. A mutable config would let a reload change a
    value underneath a call that already read it.
    """
    cls = _resolve(module_name, class_name)
    declared = _declared_configs(cls)
    assert declared, f"{class_name} declares no config class"
    for model in declared:
        assert model.model_config.get("frozen") is True, (
            f"{model.__name__} is not frozen, so a reload could mutate a "
            f"config an in-flight call already read"
        )
