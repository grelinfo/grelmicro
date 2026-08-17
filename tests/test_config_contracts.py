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
import textwrap
from pathlib import Path
from typing import Any, ForwardRef, get_args

import pytest
from pydantic import BaseModel

import grelmicro
from grelmicro._config import Reconfigurable, env_prefixes
from grelmicro.cache import TTLCache

PACKAGE_ROOT = Path(grelmicro.__file__).parent

_MIN_RECONFIGURABLES = 14
"""Exact count today, so a class dropping out of discovery fails.

Slack here would let members erode silently, which is the failure this
sweep exists to catch. Raise it deliberately when a class is added.
"""


def _module_name(path: Path) -> str:
    """Return the importable module name for a source path.

    A package is named by its directory, never by its `__init__`. Importing
    `pkg.__init__` re-executes the module under a second name, so the sweep
    would check a duplicate class object that nobody imports, and re-run any
    registration the module does at import time.
    """
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    if relative.name == "__init__":
        relative = relative.parent
    dotted = relative.as_posix().replace("/", ".")
    return f"grelmicro.{dotted}" if dotted else "grelmicro"


def _discover_reconfigurables() -> list[tuple[str, str]]:
    """Return `(module, class)` for every class declaring `Reconfigurable`."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
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


def _segment_literals(cls: Any) -> set[str]:  # noqa: ANN401
    """Return the segment strings a class passes when building its prefix.

    Read from the source rather than from `env_prefixes`, because the
    literal at the call site is what decides the published variable name.
    Calling the helper with the test's own string would only prove that
    f-strings work.
    """
    source = textwrap.dedent(inspect.getsource(cls))
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(
            node.func, "attr", None
        )
        if name not in {"env_prefixes", "default_env_prefix"}:
            continue
        for argument in node.args[:1]:
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, str
            ):
                found.add(argument.value)
    return found


@pytest.mark.parametrize(("class_name", "segment"), KIND_SEGMENTS.items())
def test_the_class_uses_the_segment_the_table_publishes(
    class_name: str, segment: str
) -> None:
    """R4: the class builds its prefix from the segment the docs publish.

    The literal at the call site is the published variable name. A rename
    there changes `GREL_TIMEOUT_*` for every deployment, so the table and
    the code have to be checked against each other, not each against
    itself.
    """
    module_name = next(
        module for module, name in RECONFIGURABLES if name == class_name
    )
    cls = _resolve(module_name, class_name)
    literals = _segment_literals(cls)
    assert segment in literals, (
        f"{class_name} builds its prefix from {sorted(literals)}, but the "
        f"published table says {segment!r}"
    )


@pytest.mark.parametrize("segment", sorted(set(KIND_SEGMENTS.values())))
def test_the_default_instance_owns_the_bare_prefix(segment: str) -> None:
    """R3: the default instance drops the name and owns the kind address.

    A named instance keeps its segment and falls back to the kind address,
    which is the row that silently stopped working in 0.38.
    """
    instance, kind = env_prefixes(segment, "default")
    assert instance == f"GREL_{segment}_"
    assert kind is None, (
        "the default instance already owns the bare prefix, so it has "
        "nothing to fall back to"
    )

    named_instance, named_kind = env_prefixes(segment, "cart")
    assert named_instance == f"GREL_{segment}_CART_"
    assert named_kind == f"GREL_{segment}_", (
        "a named instance must fall back to the kind address, which is how "
        "one variable retunes a whole kind"
    )


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
