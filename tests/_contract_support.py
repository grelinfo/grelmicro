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
    dotted = relative.as_posix().replace("/", ".")
    return f"grelmicro.{dotted}" if dotted else "grelmicro"


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


def source_files() -> list[Path]:
    """Return every Python source file in the package, in a stable order."""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def called_names(tree: ast.AST) -> set[str]:
    """Return every identifier used as a name or attribute in `tree`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        for attribute in ("id", "attr"):
            name = getattr(node, attribute, None)
            if isinstance(name, str):
                found.add(name)
    return found
