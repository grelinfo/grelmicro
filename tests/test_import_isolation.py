"""Importing a grelmicro package loads no optional dependency.

Each package is imported in a fresh interpreter, so a module another test
already loaded cannot hide one the import pulls in.
"""

from __future__ import annotations

import json
import os
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

_DEFAULT_HEALTH_TIMEOUT = 5.0
_ENV_HEALTH_TIMEOUT = 3.0


def _run(code: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
        env=env,
    )
    return result.stdout


def _loaded_after_import(module: str) -> set[str]:
    code = (
        "import json, sys\n"
        f"import {module}\n"
        "print(json.dumps(sorted({name.split('.')[0] for name in sys.modules})))"
    )
    return set(json.loads(_run(code)))


def _without_env_load() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GREL_")
    }


_BUILD_HEALTH_CHECKS = (
    "import json, sys\n"
    "from grelmicro.health import HealthChecks\n"
    "timeout = HealthChecks().config.timeout\n"
    "print(json.dumps([timeout, 'pydantic_settings' in sys.modules]))"
)
"""Build a component that resolves its config, and report what it read."""


def test_grelmicro_import_does_not_load_pydantic_settings() -> None:
    """`import grelmicro` leaves pydantic-settings unloaded."""
    # Act
    loaded = _loaded_after_import("grelmicro")

    # Assert
    assert "pydantic_settings" not in loaded


def test_config_without_env_load_does_not_load_pydantic_settings() -> None:
    """A component built with environment reads off never loads pydantic-settings."""
    # Arrange
    env = _without_env_load()
    env["GREL_HEALTH_TIMEOUT"] = "3"

    # Act
    timeout, loaded = json.loads(_run(_BUILD_HEALTH_CHECKS, env))

    # Assert
    assert timeout == _DEFAULT_HEALTH_TIMEOUT
    assert loaded is False


def test_config_with_env_load_reads_the_environment() -> None:
    """With `GREL_ENV_LOAD=1`, pydantic-settings loads and reads the variable."""
    # Arrange
    env = _without_env_load()
    env["GREL_ENV_LOAD"] = "1"
    env["GREL_HEALTH_TIMEOUT"] = "3"

    # Act
    timeout, loaded = json.loads(_run(_BUILD_HEALTH_CHECKS, env))

    # Assert
    assert timeout == _ENV_HEALTH_TIMEOUT
    assert loaded is True


def test_grelmicro_import_loads_only_the_health_endpoint_marker() -> None:
    """`import grelmicro` loads no metrics or trace module, and of health only `_served`."""
    # Arrange
    code = (
        "import json, sys\n"
        "import grelmicro\n"
        "print(json.dumps(sorted(name for name in sys.modules"
        " if name.split('.')[:2] in (['grelmicro', 'health'],"
        " ['grelmicro', 'metrics'], ['grelmicro', 'trace']))))"
    )

    # Act
    loaded = set(json.loads(_run(code)))

    # Assert
    assert loaded == {"grelmicro.health", "grelmicro.health._served"}


@pytest.mark.parametrize("module", _PUBLIC_MODULES)
def test_package_import_loads_no_optional_dependency(module: str) -> None:
    """Importing a public module loads no framework, vendor SDK or OpenTelemetry."""
    # Act
    loaded = _loaded_after_import(module)

    # Assert
    assert loaded & _OPTIONAL <= _EVERYWHERE | _USES.get(module, frozenset())


def test_log_import_loads_no_resilience_module() -> None:
    """`import grelmicro.log` loads no `grelmicro.resilience` module."""
    # Arrange
    code = (
        "import json, sys\n"
        "import grelmicro.log\n"
        "print(json.dumps(sorted(name for name in sys.modules"
        " if name.split('.')[:2] == ['grelmicro', 'resilience'])))"
    )

    # Act
    loaded = json.loads(_run(code))

    # Assert
    assert loaded == []
