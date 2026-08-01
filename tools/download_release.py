"""Download the published artifacts for a grelmicro version from PyPI.

`gh attestation verify` needs the files themselves, so they have to be on
disk before anything is installed. Run through `just verify-release`.

Uses only the standard library, so it runs before any environment is built.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/grelmicro/{version}/json"
WANTED = ("bdist_wheel", "sdist")


def download(version: str, target: Path) -> int:
    """Download the wheel and the sdist for `version` into `target`."""
    url = PYPI_JSON.format(version=version)
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        payload = json.load(response)

    files = [f for f in payload["urls"] if f["packagetype"] in WANTED]
    if not files:
        print(f"no wheel or sdist published for {version}", file=sys.stderr)
        return 1

    kinds = {f["packagetype"] for f in files}
    missing = set(WANTED) - kinds
    if missing:
        print(
            f"{version} is missing: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return 1

    for entry in files:
        destination = target / entry["filename"]
        with urllib.request.urlopen(entry["url"], timeout=60) as response:  # noqa: S310
            destination.write_bytes(response.read())
        print(f"downloaded {entry['filename']}")
    return 0


def main() -> int:
    """Parse arguments and download."""
    if len(sys.argv) != 3:  # noqa: PLR2004
        print(
            "usage: download_release.py <version> <directory>", file=sys.stderr
        )
        return 2
    return download(sys.argv[1], Path(sys.argv[2]))


if __name__ == "__main__":
    raise SystemExit(main())
