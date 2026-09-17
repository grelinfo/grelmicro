"""Naming convention checks for the grelmicro codebase.

These tests encode project-wide style rules that ruff cannot express:
exception variables must use ``error``, ``exc``, or ``ex``, never a
single-character name.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
GRELMICRO = ROOT / "grelmicro"

_SINGLE_CHAR = re.compile(r"\bas\s+[a-z]\s*:")

_EXCLUDE = {
    "mutants",
    ".claude",
    ".venv",
    "site",
    "snippets",
}


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
    violations: list[str] = []
    for path in GRELMICRO.rglob("*.py"):
        if path.name == "conftest.py":
            continue
        # Skip generated, vendored, and shared directories
        if any(seg in _EXCLUDE for seg in path.parts):
            continue
        for lineno, match in _single_char_excepts(path):
            rel = path.relative_to(ROOT)
            violations.append(f"    {rel}:{lineno}    {match}")
    assert not violations, (
        "Single-char exception variable(s) found; use ``error``, ``exc``, or ``ex``:\n"
        + "\n".join(violations)
    )
