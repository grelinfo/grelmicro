"""Check a published grelmicro version against the claims the README makes.

Run through `just verify-release <version>`, which installs the published
package into a throwaway environment first. Importing here therefore reaches
the artifact on PyPI, not the working tree.

The build-provenance check lives in the recipe rather than here, because it
runs against the downloaded files with `gh` before anything is installed.
"""

from __future__ import annotations

import sys
from importlib.metadata import version as installed_version

DOCUMENTATION_URL = "https://grelmicro.grel.info"
PYPI_JSON = "https://pypi.org/pypi/grelmicro/{version}/json"


def check_installed(expected: str) -> list[str]:
    """Return failures from resolving and importing the published version."""
    failures: list[str] = []
    found = installed_version("grelmicro")
    if found != expected:
        failures.append(f"resolved grelmicro=={found}, expected {expected}")
    try:
        import grelmicro  # noqa: F401, PLC0415
    except Exception as error:  # noqa: BLE001
        failures.append(f"importing grelmicro raised {error!r}")
    return failures


def check_metadata(expected: str) -> list[str]:
    """Return failures from the project URLs PyPI serves for the version."""
    import httpx  # noqa: PLC0415

    response = httpx.get(PYPI_JSON.format(version=expected), timeout=30)
    if response.status_code != httpx.codes.OK:
        return [f"PyPI returned {response.status_code} for {expected}"]
    urls = response.json()["info"].get("project_urls") or {}
    found = urls.get("Documentation")
    if found != DOCUMENTATION_URL:
        return [
            f"Documentation URL is {found!r}, expected {DOCUMENTATION_URL!r}"
        ]
    return []


def main() -> int:
    """Run every check and report all failures rather than the first."""
    if len(sys.argv) != 2:  # noqa: PLR2004
        print("usage: verify_release.py <version>", file=sys.stderr)
        return 2
    expected = sys.argv[1]
    failures = check_installed(expected) + check_metadata(expected)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"grelmicro=={expected} resolves, imports, and points at its docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
