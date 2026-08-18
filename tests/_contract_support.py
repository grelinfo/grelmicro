"""Shared discovery helpers for the contract sweeps.

The three sweeps each walk the package to find their family. The walking
itself is the same problem every time, and a copy per file is a copy that
drifts: the `__init__` defect that made a sweep check a duplicate class
object had to be fixed in three places at once.
"""

import ast
from pathlib import Path

import grelmicro

PACKAGE_ROOT = Path(grelmicro.__file__).parent


def module_name(path: Path) -> str:
    """Return the importable module name for a source path.

    A package is named by its directory, never by its `__init__`. Importing
    `pkg.__init__` re-executes the module under a second name, so a sweep
    would check a duplicate class object that nobody imports, and re-run any
    registration the module does at import time.
    """
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    if relative.name == "__init__":
        relative = relative.parent
    # `Path(".")` is what the package's own `__init__.py` reduces to, and its
    # `as_posix()` is "." rather than "", so a truthiness check would build
    # `grelmicro..` and every sweep would fail to import a class declared at
    # the top level.
    if relative == Path():
        return "grelmicro"
    return "grelmicro." + relative.as_posix().replace("/", ".")


def base_names(node: ast.ClassDef) -> set[str]:
    """Return the base names a class declares, by source not by import.

    Unwraps a subscript so `Reconfigurable[Config]` reports as
    `Reconfigurable`, which is what a generic protocol looks like at the
    declaration site.
    """
    names: set[str] = set()
    for base in node.bases:
        target = base
        if isinstance(target, ast.Subscript):
            target = target.value
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name:
            names.add(name)
    return names


def called_names(tree: ast.AST) -> set[str]:
    """Return every identifier used as a name or attribute in `tree`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        for attribute in ("id", "attr"):
            name = getattr(node, attribute, None)
            if isinstance(name, str):
                found.add(name)
    return found


def call_targets(tree: ast.AST) -> set[str]:
    """Return the bare names `tree` calls as plain functions.

    Following every mention would pull in a module-level function whenever
    its name appeared as an attribute or a variable, and then judge the
    caller on a body it never runs.

    Method calls are excluded for the same reason. `self._clock.monotonic()`
    is not a call to whatever `monotonic` a module happens to import, and
    resolving it against the module namespace would blame the caller for an
    unrelated function's body.
    """
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
