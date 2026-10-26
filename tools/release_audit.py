"""Print the Markdown report the maintainer reads before approving a release.

The report has four checks and a summary. Each check opens with a PASS,
WARN or FAIL line:

1. Version and changelog. Runs `tools/changelog.py check`, counts the
   entries per section, and holds a patch to fixes only. A patch with any
   Breaking, Added or Deprecated entry fails.
2. Public API diff. Builds the public surface the way
   `tests/test_public_api.py` does and compares it with the snapshot at the
   last released tag. A patch that removes or changes a name fails.
3. Deprecations. Lists every `PENDING` entry in `grelmicro/_deprecation.py`.
   An entry due at this version fails. An entry gone since the last tag
   needs a `### Breaking` changelog line.
4. CI health. Reads the latest nightly `ci.yml` run on `main` and the
   `release.yml` dry runs for `HEAD` from the public GitHub REST API. It
   sends no token, makes one call per check and reports a rate limit as
   such rather than retrying.

The summary says READY or NOT READY and names every failure. The exit code
is 0 when READY and 1 otherwise.

Run via `just release-audit <version>`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import io
import json
import re
import subprocess
import sys
import types
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import changelog  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from grelmicro._deprecation import FutureChange, Moved, Removal, Rename

    type Entry = FutureChange | Rename | Removal | Moved
    """A `PENDING` entry, from this tree or from the last tag's file."""

REPO = "grelinfo/grelmicro"
API = f"https://api.github.com/repos/{REPO}"
SNAPSHOT = (
    "tests/__snapshots__/test_public_api/test_public_api_matches_snapshot.json"
)
DEPRECATION = "grelmicro/_deprecation.py"

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

SECTIONS = (
    "Breaking",
    "Added",
    "Changed",
    "Deprecated",
    "Fixed",
    "Docs",
    "Internal",
)
"""The changelog headings counted, in the order the report lists them."""

ALIASES = {"Breaking Changes": "Breaking"}
"""Older heading spellings, counted under the current name."""

NOT_IN_PATCH = ("Breaking", "Added", "Deprecated")
"""The sections a patch release may not carry."""

FAILED = frozenset({"failure", "timed_out", "startup_failure"})
"""The run conclusions that mean the code failed, not that the run stopped."""

Status = Literal["PASS", "WARN", "FAIL"]
Kind = Literal["patch", "minor", "major"]


class GitHubError(Exception):
    """A GitHub REST call that returned no usable answer."""


@dataclass
class Check:
    """One report section and its findings."""

    title: str
    """The section heading."""

    lines: list[str] = field(default_factory=list)
    """Detail lines, rendered as Markdown under the status line."""

    failures: list[str] = field(default_factory=list)
    """What fails the release."""

    warnings: list[str] = field(default_factory=list)
    """What the maintainer should look at but does not block the release."""

    def fail(self, message: str) -> None:
        """Record a failure."""
        self.failures.append(message)

    def warn(self, message: str) -> None:
        """Record a warning."""
        self.warnings.append(message)

    @property
    def status(self) -> Status:
        """FAIL with any failure, WARN with any warning, PASS otherwise."""
        if self.failures:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "PASS"

    def render(self) -> str:
        """Return the section as Markdown."""
        out = [f"## {self.title}", "", f"**{self.status}**", ""]
        out += [f"- FAIL: {message}" for message in self.failures]
        out += [f"- WARN: {message}" for message in self.warnings]
        if self.failures or self.warnings:
            out.append("")
        out += self.lines
        return "\n".join(out).rstrip() + "\n"


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Return `(major, minor, patch)`, or None for anything but `X.Y.Z`."""
    match = SEMVER.match(version)
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def release_kind(version: str) -> Kind:
    """Return whether `version` is a patch, a minor or a major release."""
    parsed = parse_version(version)
    if parsed is None:
        msg = f"{version!r} is not MAJOR.MINOR.PATCH"
        raise ValueError(msg)
    _, minor, patch = parsed
    if patch:
        return "patch"
    if minor:
        return "minor"
    return "major"


def last_release(version: str, tags: Iterable[str]) -> str | None:
    """Return the newest `X.Y.Z` tag below `version`, or None."""
    target = parse_version(version)
    if target is None:
        return None
    found = [
        (parsed, tag)
        for tag in tags
        if (parsed := parse_version(tag)) is not None and parsed < target
    ]
    return max(found)[1] if found else None


def count_entries(body: str) -> dict[str, int]:
    """Return the number of `* ` entries under each `###` heading of `body`.

    Every heading in `SECTIONS` is present, at zero when absent. Other
    headings appear only when they hold an entry.
    """
    counts = dict.fromkeys(SECTIONS, 0)
    heading: str | None = None
    for line in body.splitlines():
        if line.startswith("### "):
            name = line[4:].strip()
            heading = ALIASES.get(name, name)
        elif line.startswith("* ") and heading is not None:
            counts[heading] = counts.get(heading, 0) + 1
    return counts


def entries_under(body: str, section: str) -> list[str]:
    """Return the entry lines under `section`, an aliased name included."""
    found: list[str] = []
    heading: str | None = None
    for line in body.splitlines():
        if line.startswith("### "):
            name = line[4:].strip()
            heading = ALIASES.get(name, name)
        elif line.startswith("* ") and heading == section:
            found.append(line)
    return found


def patch_violations(kind: Kind, counts: Mapping[str, int]) -> list[str]:
    """Return the sections a patch release carries but may not.

    Empty for a minor or a major, which may carry any section.
    """
    if kind != "patch":
        return []
    return [name for name in NOT_IN_PATCH if counts.get(name)]


def release_body(version: str, text: str) -> tuple[str, str]:
    """Return `(heading, body)` of the section that ships as `version`.

    The version's own section when the changelog is cut, else `Unreleased`.
    """
    found = changelog.sections(text)
    for heading in (version, "Unreleased"):
        if heading in found:
            return heading, found[heading][1]
    return version, ""


def changelog_check(
    version: str, last: str | None, text: str, today: str
) -> Check:
    """Check the version against the changelog and the patch policy."""
    check = Check("1. Version and changelog")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = changelog.check(version, today)
    result = ". ".join(
        line.strip() for line in output.getvalue().splitlines() if line.strip()
    )
    if code:
        check.fail(f"`changelog.py check {version}`: {result}")
    else:
        check.lines.append(f"- `changelog.py check {version}`: {result}")
    kind = release_kind(version)
    check.lines.append(f"- Version {version} is a {kind} release.")
    if last is None:
        check.warn("no earlier release tag found to compare against")
    else:
        check.lines.append(f"- Last released tag: {last}.")
    heading, body = release_body(version, text)
    counts = count_entries(body)
    check.lines += ["", f"Entries under `## {heading}`:", ""]
    check.lines += [
        "| Section | Entries |",
        "| --- | ---: |",
        *(f"| {name} | {count} |" for name, count in counts.items()),
    ]
    extra = [name for name in counts if name not in SECTIONS]
    if extra:
        check.warn(f"unexpected changelog headings: {', '.join(extra)}")
    for name in patch_violations(kind, counts):
        check.fail(
            f"a patch carries {counts[name]} {name} entries. "
            f"CONTRIBUTING.md holds a patch to fixes only."
        )
    return check


@dataclass
class ApiDiff:
    """The difference between two public API surfaces."""

    added: list[str] = field(default_factory=list)
    """New names and new members, one line each."""

    removed: list[str] = field(default_factory=list)
    """Names and members gone, one line each."""

    changed: list[str] = field(default_factory=list)
    """Signatures that differ, as `old -> new`."""


def _symbols(surface: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten `{module: {symbol: members}}` to `{module.symbol: members}`."""
    return {
        f"{module}.{symbol}": members
        for module, symbols in surface.items()
        for symbol, members in symbols.items()
    }


def api_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> ApiDiff:
    """Return what changed from the `old` public surface to `new`.

    Both are shaped like the public API snapshot. A member key such as
    `()` or `.current` is diffed on its own, so a new classmethod is an
    addition rather than a change to the class.
    """
    diff = ApiDiff()
    before, after = _symbols(old), _symbols(new)
    diff.added = [f"`{name}`" for name in sorted(after.keys() - before.keys())]
    diff.removed = [
        f"`{name}`" for name in sorted(before.keys() - after.keys())
    ]
    for name in sorted(before.keys() & after.keys()):
        was, now = before[name], after[name]
        if was == now:
            continue
        if not isinstance(was, dict) or not isinstance(now, dict):
            diff.changed.append(f"`{name}`: `{was}` -> `{now}`")
            continue
        for key in sorted(now.keys() - was.keys()):
            diff.added.append(f"`{name}` `{key}`: `{now[key]}`")
        for key in sorted(was.keys() - now.keys()):
            diff.removed.append(f"`{name}` `{key}`: `{was[key]}`")
        for key in sorted(was.keys() & now.keys()):
            if was[key] != now[key]:
                diff.changed.append(
                    f"`{name}` `{key}`: `{was[key]}` -> `{now[key]}`"
                )
    return diff


def api_check(
    version: str,
    last: str | None,
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any],
    committed: Mapping[str, Any] | None,
    breaking: int,
) -> Check:
    """Check the public API diff against the release kind."""
    check = Check("2. Public API diff")
    if committed is not None and committed != new:
        check.warn(
            "the live surface differs from the committed snapshot, so "
            "`tests/test_public_api.py` fails at HEAD"
        )
    if old is None:
        check.warn(f"no public API snapshot at tag {last}, so nothing to diff")
        return check
    diff = api_diff(old, new)
    check.lines.append(
        f"Since {last}: {len(diff.added)} added, {len(diff.removed)} "
        f"removed, {len(diff.changed)} changed."
    )
    for label, items in (
        ("Added", diff.added),
        ("Removed", diff.removed),
        ("Changed", diff.changed),
    ):
        if items:
            check.lines += ["", f"{label}:", ""]
            check.lines += [f"- {item}" for item in items]
    kind = release_kind(version)
    if kind == "patch":
        if diff.removed or diff.changed:
            check.fail(
                f"a patch removes {len(diff.removed)} and changes "
                f"{len(diff.changed)} public names or signatures"
            )
        if diff.added:
            check.warn(f"a patch adds {len(diff.added)} public names")
    elif (diff.removed or diff.changed) and not breaking:
        check.warn(
            "public names are removed or changed, and the changelog has "
            "no Breaking entry"
        )
    return check


def entry_release(entry: Entry) -> str:
    """Return the release a `PENDING` entry switches or removes in."""
    return str(
        getattr(entry, "switch_in", None) or getattr(entry, "removed_in", "")
    )


def entry_label(entry: Entry) -> str:
    """Return what a `PENDING` entry is about, as one short string.

    The retired name for a rename or a removal, and the change itself for
    a changed default.
    """
    return str(getattr(entry, "old", None) or getattr(entry, "change", ""))


def _code(text: str) -> str:
    """Return `text` as inline code, or as is when it already holds some."""
    return text if "`" in text else f"`{text}`"


def due(entries: Iterable[Entry], version: str) -> list[Entry]:
    """Return the entries whose release is at or before `version`.

    An entry whose release is not `MAJOR.MINOR.PATCH` is returned too,
    since it cannot be held to any version.
    """
    target = parse_version(version)
    found: list[Entry] = []
    for entry in entries:
        parsed = parse_version(entry_release(entry))
        if parsed is None or target is None or parsed <= target:
            found.append(entry)
    return found


def switched(old: Iterable[Entry], new: Iterable[Entry]) -> list[Entry]:
    """Return the `old` entries that no longer appear in `new`."""
    current = {(type(entry).__name__, entry_label(entry)) for entry in new}
    return [
        entry
        for entry in old
        if (type(entry).__name__, entry_label(entry)) not in current
    ]


def _needles(entry: Entry) -> set[str]:
    """Return the strings a Breaking line about `entry` would contain."""
    names = [getattr(entry, attribute, None) for attribute in ("old", "keep")]
    if getattr(entry, "old", None) is None:
        names.append(getattr(entry, "adopt", None))
    found: set[str] = set()
    for name in names:
        if name:
            found.add(name)
            found.add(re.split(r"[=.]", name, maxsplit=1)[0])
    return {needle for needle in found if needle}


def has_breaking_line(entry: Entry, breaking: Iterable[str]) -> bool:
    """Return whether any Breaking line names what `entry` retires."""
    needles = _needles(entry)
    return any(needle in line for line in breaking for needle in needles)


def deprecation_check(
    version: str,
    last: str | None,
    pending: Iterable[Entry],
    previous: Iterable[Entry] | None,
    breaking: list[str],
) -> Check:
    """Check the pending deprecations against the release."""
    check = Check("3. Deprecations")
    pending = list(pending)
    overdue = due(pending, version)
    check.lines.append(f"`PENDING` holds {len(pending)} entries:")
    check.lines.append("")
    for entry in pending:
        check.lines.append(
            f"- {type(entry).__name__} {_code(entry_label(entry))}, "
            f"in {entry_release(entry)}"
            + (" (due)" if entry in overdue else "")
        )
    if overdue:
        check.fail(
            f"{len(overdue)} entries are due at {version}. Switch each one "
            f"and drop it from `PENDING` first."
        )
    if previous is None:
        check.warn(f"could not read `{DEPRECATION}` at tag {last}")
        return check
    gone = switched(previous, pending)
    check.lines += ["", f"Switched since {last}: {len(gone)}."]
    if gone:
        check.lines.append("")
    for entry in gone:
        found = has_breaking_line(entry, breaking)
        check.lines.append(
            f"- {type(entry).__name__} {_code(entry_label(entry))}: "
            f"{'named' if found else 'not named'} in a Breaking line"
        )
        if not breaking:
            check.fail(
                f"{_code(entry_label(entry))} switched, and the changelog has "
                f"no Breaking entry"
            )
        elif not found:
            check.warn(
                f"no Breaking line names {_code(entry_label(entry))}. "
                f"Check by hand."
            )
    return check


def github(path: str) -> Any:  # noqa: ANN401
    """Return the parsed JSON of one unauthenticated GitHub REST call.

    Raises `GitHubError` on any failure. A rate limit names when it resets.
    """
    request = urllib.request.Request(  # noqa: S310  # fixed https host
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "grelmicro-release-audit",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.headers.get("X-RateLimit-Remaining") == "0":
            reset = dt.datetime.fromtimestamp(
                int(error.headers.get("X-RateLimit-Reset", "0")), tz=dt.UTC
            )
            msg = f"rate limited until {reset:%H:%M} UTC"
            raise GitHubError(msg) from error
        msg = f"HTTP {error.code}"
        raise GitHubError(msg) from error
    except (OSError, ValueError, http.client.HTTPException) as error:
        msg = f"unreachable: {error}"
        raise GitHubError(msg) from error


def _run_line(run: Mapping[str, Any]) -> str:
    """Return one workflow run as a Markdown bullet."""
    return (
        f"[{run.get('conclusion') or run.get('status')}]"
        f"({run.get('html_url')}), {run.get('created_at')}, "
        f"`{str(run.get('head_sha'))[:8]}`"
    )


def _workflow_runs(
    fetch: Callable[[str], Any], path: str
) -> list[dict[str, Any]]:
    """Return the `workflow_runs` of one runs listing."""
    answer = fetch(path)
    if not isinstance(answer, dict) or "workflow_runs" not in answer:
        msg = "the answer holds no workflow runs"
        raise GitHubError(msg)
    return answer["workflow_runs"]


def _check_nightly(check: Check, fetch: Callable[[str], Any]) -> None:
    """Record the latest scheduled `ci.yml` run on `main`."""
    try:
        runs = _workflow_runs(
            fetch,
            "/actions/workflows/ci.yml/runs"
            "?branch=main&event=schedule&per_page=1",
        )
    except GitHubError as error:
        check.warn(f"nightly not checked: {error}")
        return
    if not runs:
        check.warn("no scheduled `ci.yml` run on `main`")
        return
    nightly = runs[0]
    conclusion = nightly.get("conclusion")
    check.lines.append(f"- Latest nightly: {_run_line(nightly)}")
    if nightly.get("status") != "completed":
        check.warn("the latest nightly is still running")
    elif conclusion in FAILED:
        check.fail(f"the latest nightly concluded {conclusion}")
    elif conclusion != "success":
        check.warn(
            f"the latest nightly concluded {conclusion}, so it proves nothing"
        )


def _check_dry_runs(
    check: Check, fetch: Callable[[str], Any], sha: str
) -> None:
    """Record the manual `release.yml` runs at `sha`.

    A `release.yml` run carries no record of its inputs, so every manual
    run at `sha` counts as a dry run.
    """
    try:
        runs = _workflow_runs(
            fetch,
            "/actions/workflows/release.yml/runs"
            f"?event=workflow_dispatch&head_sha={sha}&per_page=20",
        )
    except GitHubError as error:
        check.fail(f"dry runs not checked: {error}")
        return
    check.lines.append(f"- `release.yml` dry runs at `{sha[:8]}`: {len(runs)}")
    check.lines += [f"  - {_run_line(run)}" for run in runs]
    if not any(run.get("conclusion") == "success" for run in runs):
        check.fail(f"no successful `release.yml` dry run at `{sha[:8]}`")


def _check_alerts(check: Check, fetch: Callable[[str], Any]) -> None:
    """Record the open Dependabot alerts, which GitHub serves only with a token."""
    try:
        alerts = fetch("/dependabot/alerts?state=open&per_page=100")
    except GitHubError:
        check.lines.append("- Dependabot alerts: not checked")
        return
    check.lines.append(f"- Open Dependabot alerts: {len(alerts)}")
    if alerts:
        check.warn(f"{len(alerts)} open Dependabot alerts")


def ci_check(fetch: Callable[[str], Any], sha: str) -> Check:
    """Check the nightly CI run and the release dry runs for `sha`.

    Makes one `fetch` call for each. A third call looks for open
    Dependabot alerts.
    """
    check = Check("4. CI health")
    _check_nightly(check, fetch)
    _check_dry_runs(check, fetch, sha)
    _check_alerts(check, fetch)
    return check


def summary(checks: Iterable[Check]) -> tuple[bool, str]:
    """Return whether the release is ready, and the summary as Markdown."""
    failures = [
        f"- {check.title}: {message}"
        for check in checks
        for message in check.failures
    ]
    if not failures:
        return True, "## Summary\n\n**READY**\n"
    return False, "## Summary\n\n**NOT READY**\n\n" + "\n".join(failures) + "\n"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the repository and return the result."""
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def _at_tag(tag: str | None, path: str) -> str | None:
    """Return `path` as it was at `tag`, or None when it did not exist."""
    if tag is None:
        return None
    result = _git("show", f"{tag}:{path}")
    return result.stdout if result.returncode == 0 else None


def _load_pending(source: str | None) -> tuple[Entry, ...] | None:
    """Execute a `_deprecation.py` source and return its `PENDING`.

    Returns an empty tuple when the file did not exist, and None when it
    cannot be executed against the current package.
    """
    if source is None:
        return ()
    name = "_release_audit_previous_deprecation"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, DEPRECATION, "exec"), module.__dict__)  # noqa: S102
    except Exception:  # noqa: BLE001  # an old file may import a removed name
        return None
    finally:
        del sys.modules[name]
    return tuple(module.__dict__.get("PENDING", ()))


def audit(version: str, fetch: Callable[[str], Any] = github) -> int:
    """Print the report for `version`. Returns an exit code."""
    from grelmicro._deprecation import PENDING  # noqa: PLC0415
    from tests.test_public_api import build_public_api  # noqa: PLC0415

    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    text = changelog.CHANGELOG.read_text(encoding="utf-8")
    last = last_release(version, _git("tag", "--list").stdout.split())
    sha = _git("rev-parse", "HEAD").stdout.strip()
    _, body = release_body(version, text)
    breaking = entries_under(body, "Breaking")

    old_snapshot = _at_tag(last, SNAPSHOT)
    committed = (ROOT / SNAPSHOT).read_text(encoding="utf-8")
    checks = [
        changelog_check(version, last, text, today),
        api_check(
            version,
            last,
            json.loads(old_snapshot) if old_snapshot else None,
            build_public_api(),
            json.loads(committed),
            len(breaking),
        ),
        deprecation_check(
            version,
            last,
            PENDING,
            _load_pending(_at_tag(last, DEPRECATION)),
            breaking,
        ),
        ci_check(fetch, sha),
    ]
    ready, closing = summary(checks)
    print(f"# Release audit: {version}\n")
    print(f"`HEAD` is `{sha}`, audited on {today}.\n")
    for check in checks:
        print(check.render())
    print(closing, end="")
    return 0 if ready else 1


def main() -> int:
    """Parse arguments and print the report."""
    if len(sys.argv) != 2:  # noqa: PLR2004
        print("usage: release_audit.py <version>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    if parse_version(version) is None:
        print(
            f"{version!r} is not a release version. Write 0.44.0, not v0.44.0.",
            file=sys.stderr,
        )
        return 2
    return audit(version)


if __name__ == "__main__":
    raise SystemExit(main())
