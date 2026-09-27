"""Naming convention checks for the grelmicro codebase.

These tests encode project-wide style rules that ruff cannot express:
exception variables must use ``error``, ``exc``, or ``ex``, never a
single-character name.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRELMICRO = ROOT / "grelmicro"

_SINGLE_CHAR = re.compile(r"\bexcept\b.*\bas\s+[a-z]\s*:")


def _single_char_excepts(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return ``(lineno, match)`` for single-char ``except ... as X:`` blocks."""
    text = path.read_text()
    return [
        (i + 1, line.strip())
        for i, line in enumerate(text.splitlines())
        if _SINGLE_CHAR.search(line)
    ]


def test_no_single_char_exception_names() -> None:
    """Single-character exception variables violate the ``error``/``exc``/``ex`` rule.

    Every handler in the codebase uses ``error``, ``exc``, or ``ex``.
    A single-letter name is not used anywhere else.
    """
    violations = [
        f"    {path.relative_to(ROOT)}:{lineno}    {match}"
        for path in sorted(GRELMICRO.rglob("*.py"))
        for lineno, match in _single_char_excepts(path)
    ]
    assert not violations, (
        "Single-char exception variables found. Use ``error``, ``exc``, or "
        "``ex``:\n" + "\n".join(violations)
    )


def test_the_check_reads_the_package() -> None:
    """The scan finds the package, so an empty result means something."""
    assert (GRELMICRO / "__init__.py").is_file()
