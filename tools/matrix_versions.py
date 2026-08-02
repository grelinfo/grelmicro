"""Print the Python versions the release matrix runs, one per line.

The list lives in `.github/workflows/reusable-tests.yml`, so reading it here
keeps `just test-matrix` from drifting away from what CI actually runs. A
local preflight that tests a different set of interpreters than the release
is worse than none, because it reports confidence it has not earned.

Fails loudly rather than falling back to a guess.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "reusable-tests.yml"
)

# The full matrix is the first JSON array on the `python:` matrix line, which
# reads: python: ${{ fromJSON(inputs.full && '[...]' || '[...]') }}
MATRIX = re.compile(
    r"^\s*python:\s*\$\{\{\s*fromJSON\(.*?'(\[[^']*\])'", re.MULTILINE
)


def versions() -> list[str]:
    """Return the full-matrix Python versions declared by the workflow."""
    match = MATRIX.search(WORKFLOW.read_text(encoding="utf-8"))
    if match is None:
        msg = f"no python matrix line found in {WORKFLOW}"
        raise SystemExit(msg)
    found = json.loads(match.group(1))
    if not found:
        msg = f"the python matrix in {WORKFLOW} is empty"
        raise SystemExit(msg)
    return [str(version) for version in found]


def main() -> int:
    """Print one version per line."""
    for version in versions():
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
