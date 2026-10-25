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
from typing import Any, cast

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
        "resilience",
        "security",
        "task",
        "trace",
    },
    "starlette": _COMMON
    | {
        "cache",
        "clock",
        "coordination",
        "integrations",
        "log",
        "resilience",
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
    "tasks": 167,
    "starlette": 128,
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


_ERROR_MODULES = ("grelmicro.http._authentication", "grelmicro.http._kinds")
"""Modules that raise or catch the security errors without verifying a token."""


@pytest.mark.parametrize("module", _ERROR_MODULES)
def test_security_errors_load_no_settings(module: str) -> None:
    """A module that raises or catches security errors loads no JWT verifier.

    The verifier pulls in `pydantic_settings`, which a service that never
    verifies a token should not pay for.
    """
    # Arrange
    code = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps(sorted(sys.modules)))"
    )

    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    # Assert
    loaded = set(json.loads(result.stdout))
    assert "pydantic_settings" not in loaded
    assert "grelmicro.security.jwt" not in loaded


def _run(code: str) -> object:
    """Run `code` in a fresh interpreter and return the JSON it printed last."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_starlette_integration_loads_no_route_features() -> None:
    """Importing the Starlette integration loads no authentication and no route gate."""
    # Arrange
    code = (
        "import json, sys\n"
        "import grelmicro.integrations.starlette\n"
        "print(json.dumps(sorted(sys.modules)))"
    )

    # Act
    loaded = set(cast("list[str]", _run(code)))

    # Assert
    assert "pydantic_settings" not in loaded
    assert "grelmicro.integrations._route_gate" not in loaded
    assert "grelmicro.http._authentication" not in loaded


_SERVE = """\
import json, sys
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider
{imports}
async def hello(request):
    return PlainTextResponse("hello")
micro = Grelmicro(uses=[MemoryProvider(){components}])
app = Starlette(routes=[Route("/hello", hello)])
micro.install(app)
with TestClient(app) as client:
    client.get("/hello")
    client.get("/missing")
    before = set(sys.modules)
    statuses = [client.get("/hello").status_code, client.get("/missing").status_code]
    loaded = sorted(set(sys.modules) - before)
print(json.dumps({{"statuses": statuses, "loaded": loaded}}))
"""
"""Serve a route and a missing path, then both again, and print what the second round loaded.

The first round loads what the test client itself loads on its first
request, so only the second round reads what answering loads.
"""


@pytest.mark.parametrize(
    ("imports", "components"),
    [
        pytest.param("", "", id="plain"),
        pytest.param(
            "from grelmicro.http import ErrorResponses",
            ", ErrorResponses()",
            id="error-responses",
        ),
    ],
)
def test_starlette_requests_load_no_module(
    imports: str, components: str
) -> None:
    """A Starlette app loads no module while it answers, an error response included."""
    # Arrange
    code = _SERVE.format(imports=imports, components=components)

    # Act
    printed = cast("dict[str, list[Any]]", _run(code))

    # Assert
    assert printed["statuses"] == [200, 404]
    assert printed["loaded"] == []


_FACADES = ("grelmicro.integrations.starlette",)
"""Integration modules that load some public names on first access."""


@pytest.mark.parametrize("module", _FACADES)
def test_reading_facade_annotations_loads_nothing(module: str) -> None:
    """Reading the annotations of each class a facade defines loads no grelmicro module.

    Python 3.15 `dataclasses` reads each field annotation from the module, so
    a name the lazy loader resolves would load its module at import.
    """
    # Arrange
    code = (
        "import importlib, json, sys\n"
        f"module = importlib.import_module({module!r})\n"
        "before = set(sys.modules)\n"
        "for value in list(vars(module).values()):\n"
        "    if isinstance(value, type) and value.__module__ == module.__name__:\n"
        "        for annotation in vars(value).get('__annotations__', {}).values():\n"
        "            getattr(module, str(annotation).split()[0], None)\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))"
    )

    # Act
    loaded = _run(code)

    # Assert
    assert loaded == []
