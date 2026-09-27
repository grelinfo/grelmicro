"""Naming convention checks for the grelmicro codebase.

These tests encode project-wide style rules that ruff cannot express: an
exception variable is never a single character.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRELMICRO = ROOT / "grelmicro"


def _single_char_handlers(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return `(lineno, name)` for each handler bound to a one-letter name.

    Reads the parsed code, so a handler wrapped over several lines is found
    as surely as one on a single line.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        (node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.name is not None
        and len(node.name) == 1
    ]


def test_no_single_char_exception_names() -> None:
    """An exception variable names what it holds, `error` for example."""
    violations = [
        f"    {path.relative_to(ROOT)}:{lineno}    as {name}"
        for path in sorted(GRELMICRO.rglob("*.py"))
        for lineno, name in _single_char_handlers(path)
    ]
    assert not violations, (
        "Single-char exception variables found. Name what the handler "
        "holds, `error` for example:\n" + "\n".join(violations)
    )


def test_the_check_reads_the_package() -> None:
    """The scan finds the package, so an empty result means something."""
    assert (GRELMICRO / "__init__.py").is_file()


def test_the_check_finds_a_wrapped_and_an_uppercase_handler(
    tmp_path: pathlib.Path,
) -> None:
    """A handler split over lines, or named with a capital, is still caught."""
    module = tmp_path / "module.py"
    module.write_text(
        "try:\n"
        "    pass\n"
        "except (\n"
        "    ValueError,\n"
        "    TypeError,\n"
        ") as e:\n"
        "    pass\n"
        "try:\n"
        "    pass\n"
        "except OSError as E:\n"
        "    pass\n"
    )

    assert _single_char_handlers(module) == [(3, "e"), (10, "E")]
