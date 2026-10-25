"""The reference apps load no more of grelmicro than today.

Each app from `benchmarks/memory_benchmark.py` runs in a fresh interpreter, so
a module another test already loaded cannot hide one the app pulls in.

The expected sets and counts below pin what each app loads today. Lowering
them is the goal of #1127: a change that loads less edits them down on
purpose, and a change that loads more fails here.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from benchmarks.memory_benchmark import APPS, probe

_IMPORT = frozenset({"config", "errors", "health", "providers", "types"})
"""The public subpackages `import grelmicro` loads."""

_COMMON = _IMPORT | {"metrics"}
"""The public subpackages every reference app loads."""

_EXPECTED_SUBPACKAGES = {
    "import": _IMPORT,
    "tasks": _COMMON
    | {
        "cache",
        "clock",
        "coordination",
        "http",
        "idempotency",
        "integrations",
        "log",
        "outbox",
        "resilience",
        "security",
        "task",
        "trace",
    },
    "redis": _COMMON
    | {
        "cache",
        "clock",
        "coordination",
        "http",
        "idempotency",
        "integrations",
        "log",
        "resilience",
        "security",
        "trace",
    },
    "postgres": _COMMON
    | {"cache", "clock", "coordination", "outbox", "resilience"},
}
"""The public grelmicro subpackages each case loads, by case."""

_MAX_MODULES = {
    "import": 23,
    "tasks": 169,
    "redis": 161,
    "postgres": 102,
}
"""The most grelmicro modules each case may load, by case."""


def _loaded(case: str) -> list[str]:
    """Return the grelmicro modules a case loads in a fresh interpreter."""
    if case in APPS:
        return probe(case)["grelmicro"]
    code = (
        "import json, sys\n"
        "import grelmicro\n"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )
    return [
        name
        for name in json.loads(result.stdout)
        if name.split(".")[0] == "grelmicro"
    ]


@pytest.mark.parametrize("case", sorted(_MAX_MODULES))
def test_loaded_modules_stay_within_budget(case: str) -> None:
    """A case loads exactly its pinned subpackages and at most its module count."""
    # Act
    modules = _loaded(case)

    # Assert
    subpackages = {
        name.split(".")[1]
        for name in modules
        if "." in name and not name.split(".")[1].startswith("_")
    }
    assert subpackages == _EXPECTED_SUBPACKAGES[case]
    assert len(modules) <= _MAX_MODULES[case]


_STATE_MODULES = (
    "grelmicro.resilience.circuitbreaker",
    "grelmicro.resilience.ratelimiter",
)
"""Modules with a lazy loader and a `_State` dataclass."""


@pytest.mark.parametrize("module", _STATE_MODULES)
def test_reading_state_annotations_loads_nothing(module: str) -> None:
    """Reading `_State` field annotations by name loads no grelmicro module.

    Python 3.15 `dataclasses` reads each field annotation from the module, so
    a name the lazy loader resolves would load its module at import.
    """
    # Arrange
    code = (
        "import importlib, json, sys\n"
        f"module = importlib.import_module({module!r})\n"
        "before = set(sys.modules)\n"
        "for annotation in module._State.__annotations__.values():\n"
        "    getattr(module, annotation.split()[0], None)\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))"
    )

    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    # Assert
    assert json.loads(result.stdout) == []
