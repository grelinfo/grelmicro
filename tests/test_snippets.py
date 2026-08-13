"""Execute documentation snippets to catch import and runtime drift.

`compileall` and the MkDocs build only check that snippets parse. This
module goes one step further and imports each snippet so import drift,
renamed symbols, and module-level runtime errors surface as test
failures.

Snippets are tiered:

- `RUN`: executed with no special setup (the default).
- `ENV`: executed with the documented environment variables set.
- `COMPILE_ONLY`: parsed but not executed, because running them at
  import time has global side effects (they call `asyncio.run(...)`).
  These are still covered by `compileall` and the MkDocs build.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
_SNIPPETS_DIR = _DOCS_DIR / "snippets"

# `--8<-- "path/to/snippet.py"`, optionally with a `:start:end` line range.
_INCLUDE_RE = re.compile(r'--8<--\s*"([^":]+)(?::\d+)*"')

# Snippets whose module body runs an event loop with global side effects
# (logging / tracing setup). Compiled and built by MkDocs, not run here.
_COMPILE_ONLY = {
    "trace/component.py",
    "trace/autoinstrument.py",
    "log/component.py",
}

# Snippets that read required configuration from the environment. They
# are run with the documented variables set.
_ENV = {
    "resilience/fallback_environmental.py": {
        "GREL_FALLBACK_RECS_WHEN": "builtins.ValueError",
        "GREL_FALLBACK_RECS_DEFAULT": "[]",
    },
    "resilience/timeout_environmental.py": {
        "GREL_TIMEOUT_DB_SECONDS": "2.0",
    },
    "coordination/postgres.py": {
        "POSTGRES_URL": "postgresql://user:password@localhost:5432/db",
    },
    "deployment/composition_root.py": {
        "REDIS_URL": "redis://localhost:6379/0",
    },
}

_ALL = sorted(
    p.relative_to(_SNIPPETS_DIR).as_posix() for p in _SNIPPETS_DIR.rglob("*.py")
)
_RUNNABLE = [rel for rel in _ALL if rel not in _COMPILE_ONLY]


def _import_snippet(rel: str) -> None:
    path = _SNIPPETS_DIR / rel
    spec = importlib.util.spec_from_file_location(
        f"snippet_{rel.replace('/', '_').removesuffix('.py')}", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_snippets_present() -> None:
    """The snippet tiers reference files that still exist."""
    for rel in _COMPILE_ONLY | set(_ENV):
        assert (_SNIPPETS_DIR / rel).is_file(), rel


def test_no_orphan_snippets() -> None:
    """Every snippet is included by a page.

    `check_paths` fails an include pointing at a missing file. Nothing
    catches the reverse, so a snippet no page renders still costs test
    time and reads as a live example to anyone browsing the tree.
    """
    included = {
        match
        for page in _DOCS_DIR.rglob("*.md")
        for match in _INCLUDE_RE.findall(page.read_text())
    }
    snippets = {rel for rel in _ALL if not rel.endswith("__init__.py")}
    orphans = sorted(snippets - included)
    assert not orphans, (
        f"snippets included by no page: {orphans}. Include them or delete them."
    )


@pytest.mark.parametrize("rel", _RUNNABLE)
def test_snippet_imports(rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each runnable snippet imports without error."""
    for key, value in _ENV.get(rel, {}).items():
        monkeypatch.setenv(key, value)
    _import_snippet(rel)
