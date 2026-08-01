"""Download the published artifacts for a grelmicro version from PyPI.

`gh attestation verify` needs the files themselves, so they have to be on
disk before anything is installed. Run through `just verify-release`.

Uses only the standard library, so it runs before any environment is built.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/grelmicro/{version}/json"
WANTED = ("bdist_wheel", "sdist")


def fetch(url: str, timeout: int) -> bytes | None:
    """Return the body at `url`, or None after reporting why it failed."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return bytes(response.read())
    except urllib.error.HTTPError as error:
        print(f"{url} returned HTTP {error.code}", file=sys.stderr)
    except urllib.error.URLError as error:
        print(f"could not reach {url}: {error.reason}", file=sys.stderr)
    return None


def download(version: str, target: Path) -> int:
    """Download the wheel and the sdist for `version` into `target`."""
    target.mkdir(parents=True, exist_ok=True)
    body = fetch(PYPI_JSON.format(version=version), timeout=30)
    if body is None:
        print(f"no published release for grelmicro=={version}", file=sys.stderr)
        return 1
    payload = json.loads(body)

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
        content = fetch(entry["url"], timeout=60)
        if content is None:
            print(f"could not download {entry['filename']}", file=sys.stderr)
            return 1
        (target / entry["filename"]).write_bytes(content)
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
