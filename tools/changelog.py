"""Read and write a version's section in `docs/changelog.md`.

Three release steps need the same parsing. `release` renames the
`Unreleased` heading to the version being cut, which was the one manual
step in the procedure and the easiest to get wrong. `check` then gates the
release on that section existing and carrying today's date, so a tag is
never cut against a changelog that still says `Unreleased` or carries
yesterday's date. `notes` prints the section body, which is what the GitHub
Release description should hold.

The writer and the checker live together on purpose: one parser, so they
cannot disagree about the format.

Run via `just release`, `just release-check` and `just release-notes`.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "docs" / "changelog.md"

HEADING = re.compile(
    r"^## (?P<version>\S+)(?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)


def sections(text: str) -> dict[str, tuple[str | None, str]]:
    """Return `{version: (date, body)}` for every section in the changelog."""
    found: dict[str, tuple[str | None, str]] = {}
    version: str | None = None
    date: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            if version is not None:
                found[version] = (date, "\n".join(body).strip())
            version = match["version"]
            date = match["date"]
            body = []
            continue
        if version is not None:
            body.append(line)
    if version is not None:
        found[version] = (date, "\n".join(body).strip())
    return found


def check(version: str, today: str) -> int:
    """Report whether `version` is ready to tag. Returns an exit code."""
    found = sections(CHANGELOG.read_text(encoding="utf-8"))
    if version not in found:
        print(f"changelog has no section for {version}", file=sys.stderr)
        released = [v for v, (d, _) in found.items() if d]
        if released:
            print(f"latest released section: {released[0]}", file=sys.stderr)
        if "Unreleased" in found:
            print(
                f"rename the 'Unreleased' heading to '## {version} - {today}'",
                file=sys.stderr,
            )
        return 1
    date, body = found[version]
    if date is None:
        print(f"section for {version} carries no date", file=sys.stderr)
        return 1
    if date != today:
        print(
            f"section for {version} is dated {date}, not today ({today})",
            file=sys.stderr,
        )
        return 1
    if not body:
        print(f"section for {version} is empty", file=sys.stderr)
        return 1
    print(f"changelog ready: {version} dated {date}")
    return 0


def release(version: str, today: str) -> int:
    """Rename the `Unreleased` heading to `version`. Returns an exit code.

    Idempotent when the section already carries today's date, so re-running
    after a failed step later in the procedure is safe. Refuses when the
    section is dated for a different day, since that is a section already
    cut rather than one waiting to be.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    found = sections(text)
    if version in found:
        date = found[version][0]
        if date == today:
            print(f"changelog already cut: {version} dated {today}")
            return 0
        print(
            f"changelog already has a section for {version}, dated {date}. "
            f"Pick the next version, a released one is never rewritten.",
            file=sys.stderr,
        )
        return 1
    if "Unreleased" not in found:
        print(
            "changelog has no 'Unreleased' section to cut. Add entries "
            "under '## Unreleased' first.",
            file=sys.stderr,
        )
        return 1
    if not found["Unreleased"][1]:
        print("the 'Unreleased' section is empty", file=sys.stderr)
        return 1
    updated = text.replace("## Unreleased\n", f"## {version} - {today}\n", 1)
    CHANGELOG.write_text(updated, encoding="utf-8")
    print(f"changelog cut: {version} dated {today}")
    return 0


def notes(version: str) -> int:
    """Print the body of `version`'s section. Returns an exit code."""
    found = sections(CHANGELOG.read_text(encoding="utf-8"))
    if version not in found:
        print(f"changelog has no section for {version}", file=sys.stderr)
        return 1
    print(found[version][1])
    return 0


def main() -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("release", "check", "notes"):
        item = sub.add_parser(name)
        item.add_argument("version")
    args = parser.parse_args()
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    if args.command == "release":
        return release(args.version, today)
    if args.command == "check":
        return check(args.version, today)
    return notes(args.version)


if __name__ == "__main__":
    raise SystemExit(main())
