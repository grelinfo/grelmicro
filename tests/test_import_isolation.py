"""Importing a grelmicro package loads no optional dependency.

Each package is imported in a fresh interpreter, so a module another test
already loaded cannot hide one the import pulls in.
"""

from __future__ import annotations

import json
import pkgutil
import subprocess
import sys

import pytest

import grelmicro

_OPTIONAL = frozenset(
    {
        "aiosqlite",
        "asyncpg",
        "cryptography",
        "fastapi",
        "faststream",
        "httpx",
        "litestar",
        "lightkube",
        "loguru",
        "opentelemetry",
        "orjson",
        "redis",
        "sqlalchemy",
        "starlette",
        "structlog",
        "valkey",
        "yaml",
    }
)
"""Top-level modules of the optional dependencies a module could load."""

_PUBLIC_MODULES = sorted(
    f"grelmicro.{info.name}"
    for info in pkgutil.iter_modules(grelmicro.__path__)
    if not info.name.startswith("_")
    and info.name not in {"integrations", "providers"}
)
"""The public modules, without the framework and vendor entry points."""

_EVERYWHERE = frozenset({"orjson"})
"""Optional dependencies every module loads at import when they are installed.

orjson, from the `standard` extra, handles the JSON grelmicro reads and writes.
"""

_USES = {"grelmicro.log": frozenset({"opentelemetry"})}
"""Optional dependencies one module loads at import when they are installed.

`grelmicro.log` loads OpenTelemetry to add the trace and span ids to each record.
"""


def _loaded_after_import(module: str) -> set[str]:
    code = (
        "import json, sys\n"
        f"import {module}\n"
        "print(json.dumps(sorted({name.split('.')[0] for name in sys.modules})))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )
    return set(json.loads(result.stdout))


@pytest.mark.parametrize("module", _PUBLIC_MODULES)
def test_package_import_loads_no_optional_dependency(module: str) -> None:
    """Importing a public module loads no framework, vendor SDK or OpenTelemetry."""
    # Act
    loaded = _loaded_after_import(module)

    # Assert
    assert loaded & _OPTIONAL <= _EVERYWHERE | _USES.get(module, frozenset())
