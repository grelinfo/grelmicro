"""An integration refuses at import a framework older than its extra allows.

Each import runs in a fresh interpreter that reports an older framework
version, so no module another test already loaded hides the check.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from grelmicro import DependencyVersionError, GrelmicroError
from grelmicro.integrations._floor import (
    FASTAPI_FLOOR,
    STARLETTE_FLOOR,
    require,
)

_ROOT = Path(__file__).parent.parent
"""The repository root."""

_EXTRAS = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
    "optional-dependencies"
]
"""The extras the project declares, by name."""


def _import_with(
    module: str,
    versions: dict[str, str],
    *,
    hidden: tuple[str, ...] = (),
) -> str:
    """Import `module` with each framework reporting the given version.

    Each module in `hidden` imports as if it were not installed. Returns
    what the interpreter printed: `imported`, or the error raised.
    """
    patches = "".join(
        f"import {name}\n{name}.__version__ = {version!r}\n"
        for name, version in versions.items()
    )
    hides = "".join(f"sys.modules[{name!r}] = None\n" for name in hidden)
    code = (
        "import sys\n"
        f"{patches}"
        f"{hides}"
        "try:\n"
        f"    import {module}\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__, error)\n"
        "else:\n"
        "    print('imported')\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _framework(name: str, version: str | None) -> ModuleType:
    """Return a module named `name` whose `__version__` is `version`."""
    framework = ModuleType(name)
    if version is not None:
        framework.__version__ = version  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    return framework


def test_fastapi_integration_refuses_an_older_starlette() -> None:
    """The FastAPI integration names the Starlette floor and its own extra."""
    # Act
    printed = _import_with(
        "grelmicro.integrations.fastapi", {"starlette": "1.3.1"}
    )

    # Assert
    assert printed == (
        "DependencyVersionError Module starlette 1.3.1 is older than 1.7.0, "
        "try running 'pip install \"grelmicro[fastapi]\"'"
    )


def test_fastapi_integration_refuses_an_older_fastapi() -> None:
    """The FastAPI integration names the FastAPI floor and its own extra."""
    # Act
    printed = _import_with(
        "grelmicro.integrations.fastapi", {"fastapi": "0.135.0"}
    )

    # Assert
    assert printed == (
        "DependencyVersionError Module fastapi 0.135.0 is older than 0.142.1, "
        "try running 'pip install \"grelmicro[fastapi]\"'"
    )


def test_fastapi_integration_refuses_an_older_fastapi_before_other_imports() -> (
    None
):
    """An import that fails after FastAPI loads does not hide its version."""
    # Act
    printed = _import_with(
        "grelmicro.integrations.fastapi",
        {"fastapi": "0.135.0"},
        hidden=("opentelemetry",),
    )

    # Assert
    assert printed == (
        "DependencyVersionError Module fastapi 0.135.0 is older than 0.142.1, "
        "try running 'pip install \"grelmicro[fastapi]\"'"
    )


def test_fastapi_integration_imports_without_fastapi() -> None:
    """Without FastAPI, the FastAPI integration still imports."""
    # Act
    printed = _import_with(
        "grelmicro.integrations.fastapi", {}, hidden=("fastapi",)
    )

    # Assert
    assert printed == "imported"


@pytest.mark.parametrize(
    "module",
    ["grelmicro.integrations.starlette", "grelmicro.integrations._route_gate"],
)
def test_starlette_imports_refuse_an_older_starlette(module: str) -> None:
    """The Starlette integration and its route gate name the starlette extra.

    FastAPI stays installed, so the extra does not depend on what else is.
    """
    # Act
    printed = _import_with(module, {"starlette": "1.3.1"})

    # Assert
    assert printed == (
        "DependencyVersionError Module starlette 1.3.1 is older than 1.7.0, "
        "try running 'pip install \"grelmicro[starlette]\"'"
    )


@pytest.mark.parametrize(
    "version",
    ["1.7", "1.7.0", "1.7.0+local", "1.7.0.post1", "1.10.0", "2.0"],
)
def test_require_passes_the_floor_and_newer(version: str) -> None:
    """A release at the floor or newer passes, compared number by number."""
    # Act
    require(_framework("starlette", version), "1.7.0", extra="starlette")


@pytest.mark.parametrize(
    "version",
    [
        "1.6.9",
        "1.6",
        "1.7.0rc1",
        "1.7.0a1",
        "1.7.0.dev0",
        "1.7.0b2+local",
    ],
)
def test_require_refuses_older_and_floor_prereleases(version: str) -> None:
    """A release below the floor, or a pre-release of it, is refused."""
    # Act
    with pytest.raises(DependencyVersionError) as caught:
        require(_framework("starlette", version), "1.7.0", extra="starlette")

    # Assert
    assert str(caught.value) == (
        f"Module starlette {version} is older than 1.7.0, "
        "try running 'pip install \"grelmicro[starlette]\"'"
    )


def test_require_refuses_a_prerelease_of_the_fastapi_floor() -> None:
    """A dev build of the FastAPI floor is refused, as the pin refuses it."""
    # Act
    with pytest.raises(DependencyVersionError):
        require(
            _framework("fastapi", "0.142.1.dev0"), "0.142.1", extra="fastapi"
        )


def test_require_reads_the_installed_version_without_dunder_version() -> None:
    """A module without `__version__` is checked by its installed version."""
    # Act
    require(_framework("starlette", None), "1.7.0", extra="starlette")

    # Assert
    with pytest.raises(DependencyVersionError):
        require(_framework("starlette", None), "99.0", extra="starlette")


def test_require_refuses_a_version_without_a_number() -> None:
    """A version with no release number reads as no version."""
    # Act
    with pytest.raises(DependencyVersionError) as caught:
        require(_framework("starlette", "unknown"), "1.7.0", extra="starlette")

    # Assert
    assert str(caught.value) == (
        "Could not read the version of module starlette, "
        "try running 'pip install \"grelmicro[starlette]\"'"
    )


def test_require_refuses_a_module_with_no_version_at_all() -> None:
    """A module with no version to read is refused as a version error."""
    # Act
    with pytest.raises(DependencyVersionError) as caught:
        require(_framework("nowhere", None), "1.0", extra="starlette")

    # Assert
    assert str(caught.value) == (
        "Could not read the version of module nowhere, "
        "try running 'pip install \"grelmicro[starlette]\"'"
    )


def test_dependency_version_error_is_a_grelmicro_import_error() -> None:
    """The error is caught as a `GrelmicroError` and as an `ImportError`."""
    # Assert
    assert issubclass(DependencyVersionError, GrelmicroError)
    assert issubclass(DependencyVersionError, ImportError)


@pytest.mark.parametrize("extra", ["fastapi", "starlette"])
def test_installation_page_lists_each_framework_floor(extra: str) -> None:
    """The extras table on the installation page lists the pins of the extra."""
    # Arrange
    page = (_ROOT / "docs" / "installation.md").read_text()
    row = re.search(rf"^\| `{extra}` \|.*$", page, re.MULTILINE)

    # Assert
    assert row is not None
    for pin in _EXTRAS[extra]:
        assert f"`{pin}`" in row.group()


def test_floors_match_the_extras() -> None:
    """The floors checked at import are the ones the extras declare."""
    # Assert
    assert f"fastapi>={FASTAPI_FLOOR}" in _EXTRAS["fastapi"]
    assert f"starlette>={STARLETTE_FLOOR}" in _EXTRAS["fastapi"]
    assert f"starlette>={STARLETTE_FLOOR}" in _EXTRAS["starlette"]
