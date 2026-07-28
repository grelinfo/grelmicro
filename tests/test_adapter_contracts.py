"""Structural checks that every first-party adapter honors its protocol.

The `_loop` contract is invisible to the type checkers: a `Protocol`
attribute annotation declares the requirement but never creates the
attribute, so an adapter that omits it type-checks and then raises
`AttributeError` on the first `from_thread` call. These tests read the
source instead.
"""

import ast
from pathlib import Path

import pytest

import grelmicro

LOOP_PROTOCOLS = frozenset(
    {
        "CacheBackend",
        "CircuitBreakerBackend",
        "LockBackend",
        "ScheduleBackend",
    }
)

PACKAGE_ROOT = Path(grelmicro.__file__).parent

_MIN_ADAPTERS = 10
"""Floor for the adapter sweep, so an empty scan cannot pass silently."""


def _adapters_declaring(protocols: frozenset[str]) -> list[tuple[str, str]]:
    """Return `(module, class)` for classes based on any named protocol."""
    found = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if bases & protocols:
                module = path.relative_to(PACKAGE_ROOT.parent).as_posix()
                found.append((module, node.name))
    return found


def _class_source(module: str, name: str) -> str:
    """Return the source of `name` as defined in `module`."""
    path = PACKAGE_ROOT.parent / module
    source = path.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    pytest.fail(f"{name} not found in {module}")


def test_the_sweep_finds_adapters() -> None:
    """The scan itself works, so an empty result cannot pass silently."""
    # Act
    adapters = _adapters_declaring(LOOP_PROTOCOLS)

    # Assert
    assert len(adapters) > _MIN_ADAPTERS
    assert ("grelmicro/coordination/postgres.py", "PostgresLockAdapter") in (
        adapters
    )


@pytest.mark.parametrize(
    ("module", "name"), _adapters_declaring(LOOP_PROTOCOLS)
)
def test_adapter_captures_the_running_loop(module: str, name: str) -> None:
    """Every adapter initializes `_loop` and captures it on `__aenter__`.

    `Lock.from_thread`, `TaskLock.from_thread`, `@cached`, and the circuit
    breaker all dispatch into `backend._loop`. An adapter that never sets it
    raises `AttributeError` instead of the clear backend-not-open error.
    """
    # Arrange
    source = _class_source(module, name)

    # Assert
    assert "self._loop: asyncio.AbstractEventLoop | None = None" in source, (
        f"{name} does not initialize `_loop` in `__init__`"
    )
    assert "self._loop = asyncio.get_running_loop()" in source, (
        f"{name} does not capture the running loop in `__aenter__`"
    )
