"""Type checkers see every public name a lazy module serves, and nothing else.

A module-level `__getattr__` that type checkers can see types any missing
name as `object`, so an import of a removed name passes the check. Each
module keeps its `__getattr__` behind `if not TYPE_CHECKING:` and binds every
name it serves where type checkers read it.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import grelmicro

_PACKAGE_ROOT = Path(grelmicro.__file__).parent


def _module_name(path: Path) -> str:
    parts = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _is_not_type_checking(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_type_checking(test.operand)
    )


def _checked(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return the module-level statements a type checker reads in `body`."""
    statements: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.If):
            if _is_type_checking(node.test):
                statements.extend(_checked(node.body))
            elif _is_not_type_checking(node.test):
                statements.extend(_checked(node.orelse))
            else:
                statements.extend(_checked(node.body))
                statements.extend(_checked(node.orelse))
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                statements.extend(_checked(block))
            for handler in node.handlers:
                statements.extend(_checked(handler.body))
        else:
            statements.append(node)
    return statements


def _targets(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        return {name for item in target.elts for name in _targets(item)}
    return set()


def _bindings(statements: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """Return the names bound, and the names a type checker re-exports.

    An import re-exports a name only in the `name as name` form. Every other
    binding counts as an export.
    """
    bound: set[str] = set()
    exported: set[str] = set()
    for node in statements:
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bound.add(name)
                if alias.asname == alias.name.split(".")[-1]:
                    exported.add(name)
        elif isinstance(
            node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            bound.add(node.name)
            exported.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bound |= _targets(target)
                exported |= _targets(target)
        elif isinstance(node, ast.AnnAssign):
            bound |= _targets(node.target)
            exported |= _targets(node.target)
        elif isinstance(node, ast.TypeAlias):
            bound.add(node.name.id)
            exported.add(node.name.id)
    return bound, exported


def _defines_getattr(body: list[ast.stmt]) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        for node in body
    ) or any(
        isinstance(node, ast.If) and _defines_getattr(node.body + node.orelse)
        for node in body
    )


def _lazy_modules() -> list[str]:
    modules = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _defines_getattr(tree.body):
            modules.append(_module_name(path))
    return modules


_LAZY_MODULES = _lazy_modules()
"""Every grelmicro module with a module-level `__getattr__`."""


def _served(name: str) -> set[str]:
    """Return every public name `name` serves, eagerly or lazily."""
    module = importlib.import_module(name)
    served: set[str] = set(getattr(module, "__all__", ()))
    served |= set(getattr(module, "_LAZY", {}))
    served |= set(getattr(module, "_SUBMODULES", ()))
    return served


def _tree(name: str) -> ast.Module:
    module = importlib.import_module(name)
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_every_lazy_module_is_found() -> None:
    """The scan finds the lazy modules, the root package among them."""
    # Assert
    assert "grelmicro" in _LAZY_MODULES
    assert "grelmicro.integrations.fastapi" in _LAZY_MODULES


@pytest.mark.parametrize("name", _LAZY_MODULES)
def test_type_checkers_do_not_see_getattr(name: str) -> None:
    """The module-level `__getattr__` sits behind `if not TYPE_CHECKING:`."""
    # Arrange
    statements = _checked(_tree(name).body)

    # Act
    bound, _ = _bindings(statements)

    # Assert
    assert "__getattr__" not in bound


@pytest.mark.parametrize("name", _LAZY_MODULES)
def test_type_checkers_see_every_served_name(name: str) -> None:
    """Each public name is bound where type checkers read it, and exported."""
    # Arrange
    module = importlib.import_module(name)
    listed = set(getattr(module, "__all__", ()))
    bound, exported = _bindings(_checked(_tree(name).body))

    # Act
    served = _served(name)

    # Assert
    assert sorted(served - bound) == []
    assert sorted(served - listed - exported) == []
