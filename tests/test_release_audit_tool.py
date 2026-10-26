"""The release audit reports what would make a release wrong.

A release tag is immutable, so the audit is read before one is cut. These
hold its pure parts to the release policy: a patch carries fixes only and
keeps the public API, a deprecation due at the version blocks it, and the
CI checks read the GitHub answers they are given.
"""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from grelmicro._deprecation import FutureChange, Removal, Rename
from tools import changelog, release_audit
from tools.release_audit import GitHubError

TODAY = dt.datetime.now(tz=dt.UTC).date().isoformat()

BODY = """### Breaking Changes
* 💥 `OldName` is removed. Use `NewName`.

### Added
* ✨ One.
* ✨ Two.

### Fixed
* 🐛 A fix.

### Docs
* 📝 A page.
"""

OLD_API = {
    "grelmicro": {
        "Gone": None,
        "Kept": {"()": "(a) -> None"},
        "Lock": {"()": "(name, *, timeout=1.0) -> None"},
    },
    "grelmicro.cache": {"Cache": {"()": "() -> None"}},
}

NEW_API = {
    "grelmicro": {
        "Kept": {"()": "(a) -> None"},
        "Lock": {
            "()": "(name, *, timeout=2.0) -> None",
            ".from_url": "(url) -> Lock",
        },
        "New": None,
    },
    "grelmicro.cache": {"Cache": {"()": "() -> None"}},
}

ENUM = Removal(old="OldEnum", use="the string", removed_in="0.44.0")
NAME = Rename(old="auto_health=True", new="checks=", removed_in="0.45.0")
GATE = FutureChange(
    change="`Cron(gate=)` defaults to `leader`",
    switch_in="0.44.0",
    keep="gate=None",
    adopt="gate=leader",
)


@pytest.mark.parametrize(
    ("version", "kind"),
    [("0.43.1", "patch"), ("0.44.0", "minor"), ("1.0.0", "major")],
)
def test_release_kind_reads_the_version(version: str, kind: str) -> None:
    """A non-zero patch number is a patch, else the minor decides."""
    assert release_audit.release_kind(version) == kind


def test_last_release_is_the_newest_tag_below_the_version() -> None:
    """Tags that are not `X.Y.Z` and tags at or above the version are skipped."""
    tags = ["0.42.7", "0.43.1", "0.43.0", "core-0.1.0", "0.44.0", "0.9.0"]
    assert release_audit.last_release("0.44.0", tags) == "0.43.1"
    assert release_audit.last_release("0.1.0", tags) is None


def test_count_entries_counts_every_section() -> None:
    """Every counted heading is present, and an old spelling counts too."""
    counts = release_audit.count_entries(BODY)
    assert counts == {
        "Breaking": 1,
        "Added": 2,
        "Changed": 0,
        "Deprecated": 0,
        "Fixed": 1,
        "Docs": 1,
        "Internal": 0,
    }


def test_count_entries_keeps_an_unexpected_heading() -> None:
    """A heading outside the list is counted, so the report can name it."""
    counts = release_audit.count_entries("### Security\n* 🔒 A fix.\n")
    assert counts["Security"] == 1


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("patch", ["Breaking", "Added"]),
        ("minor", []),
        ("major", []),
    ],
)
def test_patch_violations_hold_a_patch_to_fixes(
    kind: release_audit.Kind, expected: list[str]
) -> None:
    """A patch may not carry Breaking, Added or Deprecated entries."""
    counts = release_audit.count_entries(BODY)
    assert release_audit.patch_violations(kind, counts) == expected


def test_a_patch_of_fixes_only_passes_the_policy() -> None:
    """Fixed, Docs and Internal entries are what a patch is for."""
    counts = release_audit.count_entries(
        "### Fixed\n* 🐛 x\n### Internal\n* y\n"
    )
    assert release_audit.patch_violations("patch", counts) == []


@pytest.fixture
def changelog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the changelog tool at a throwaway changelog."""
    text = f"# Changelog\n\n## 0.43.2 - {TODAY}\n\n{BODY}\n## 0.43.1 - 2026-10-25\n"
    path = tmp_path / "changelog.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", path)
    return text


def test_changelog_check_fails_a_patch_with_added_entries(
    changelog_file: str,
) -> None:
    """The cut section passes the date check and still fails the policy."""
    check = release_audit.changelog_check(
        "0.43.2", "0.43.1", changelog_file, TODAY
    )
    assert check.status == "FAIL"
    assert [message.split()[4] for message in check.failures] == [
        "Breaking",
        "Added",
    ]
    assert any("changelog ready" in line for line in check.lines)


def test_changelog_check_fails_an_uncut_changelog(changelog_file: str) -> None:
    """A version with no section fails with what `check` said."""
    check = release_audit.changelog_check(
        "0.44.0", "0.43.1", changelog_file, TODAY
    )
    assert "no section for 0.44.0" in check.failures[0]


def test_api_diff_lists_added_removed_and_changed() -> None:
    """A new member is an addition, and only a differing value is a change."""
    diff = release_audit.api_diff(OLD_API, NEW_API)
    assert diff.added == [
        "`grelmicro.New`",
        "`grelmicro.Lock` `.from_url`: `(url) -> Lock`",
    ]
    assert diff.removed == ["`grelmicro.Gone`"]
    assert diff.changed == [
        (
            "`grelmicro.Lock` `()`: `(name, *, timeout=1.0) -> None` -> "
            "`(name, *, timeout=2.0) -> None`"
        )
    ]


def test_api_diff_of_a_removed_module_removes_its_names() -> None:
    """Every name of a module that is gone is reported removed."""
    diff = release_audit.api_diff(OLD_API, {"grelmicro": OLD_API["grelmicro"]})
    assert diff.removed == ["`grelmicro.cache.Cache`"]


def test_api_diff_of_equal_surfaces_is_empty() -> None:
    """The same surface twice reports nothing."""
    diff = release_audit.api_diff(OLD_API, OLD_API)
    assert (diff.added, diff.removed, diff.changed) == ([], [], [])


def test_api_check_fails_a_patch_that_changes_the_api() -> None:
    """A patch that removes or changes a name fails."""
    check = release_audit.api_check(
        "0.43.2", "0.43.1", OLD_API, NEW_API, NEW_API, breaking=0
    )
    assert check.status == "FAIL"


def test_api_check_lets_a_minor_change_the_api_with_a_breaking_entry() -> None:
    """A minor may break, as long as the changelog says so."""
    check = release_audit.api_check(
        "0.44.0", "0.43.1", OLD_API, NEW_API, NEW_API, breaking=1
    )
    assert check.status == "PASS"


def test_api_check_warns_on_a_stale_snapshot() -> None:
    """A live surface that differs from the committed snapshot is named."""
    check = release_audit.api_check(
        "0.44.0", "0.43.1", NEW_API, NEW_API, OLD_API, breaking=0
    )
    assert check.status == "WARN"


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.43.9", []), ("0.44.0", [ENUM, GATE]), ("0.45.0", [ENUM, NAME, GATE])],
)
def test_due_flags_entries_at_or_before_the_version(
    version: str, expected: list[FutureChange | Rename | Removal]
) -> None:
    """An entry is due from the release it names onward."""
    assert release_audit.due([ENUM, NAME, GATE], version) == expected


def test_switched_lists_the_entries_gone_since_the_tag() -> None:
    """An entry in the old `PENDING` and not in the new one switched."""
    assert release_audit.switched([ENUM, NAME, GATE], [NAME]) == [ENUM, GATE]


@pytest.mark.parametrize(
    ("entry", "line"),
    [
        (ENUM, "* 💥 `OldEnum` is removed."),
        (NAME, "* 💥 `auto_health=` is removed."),
        (GATE, "* 💥 `Cron` gates on the leader. Pass `gate=None` to opt out."),
    ],
)
def test_has_breaking_line_finds_the_retired_name(
    entry: FutureChange | Rename | Removal, line: str
) -> None:
    """A Breaking line naming what the entry retires counts."""
    assert release_audit.has_breaking_line(entry, [line])


def test_has_breaking_line_ignores_an_unrelated_line() -> None:
    """A Breaking line about something else does not count."""
    assert not release_audit.has_breaking_line(ENUM, ["* 💥 Other."])


def test_deprecation_check_fails_on_a_due_entry() -> None:
    """A version that should switch an entry cannot ship while it warns."""
    check = release_audit.deprecation_check(
        "0.44.0", "0.43.1", [ENUM, NAME], [ENUM, NAME], []
    )
    assert check.failures == [
        (
            "1 entries are due at 0.44.0. Switch each one and drop it from "
            "`PENDING` first."
        )
    ]


def test_deprecation_check_fails_a_switch_with_no_breaking_entry() -> None:
    """An entry gone since the tag needs a Breaking line."""
    check = release_audit.deprecation_check(
        "0.44.0", "0.43.1", [NAME], [ENUM, NAME], []
    )
    assert check.status == "FAIL"
    named = release_audit.deprecation_check(
        "0.44.0", "0.43.1", [NAME], [ENUM, NAME], ["* 💥 `OldEnum` is gone."]
    )
    assert named.status == "PASS"


def _fetch(answers: dict[str, Any]) -> Any:  # noqa: ANN401
    """Return a fetch that answers by path prefix, raising for an error."""

    def fetch(path: str) -> Any:  # noqa: ANN401
        for prefix, answer in answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        msg = "HTTP 401"
        raise GitHubError(msg)

    return fetch


def _runs(*conclusions: str) -> dict[str, list[dict[str, str]]]:
    """Return a workflow runs payload with one completed run per conclusion."""
    return {
        "workflow_runs": [
            {
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": "a" * 40,
                "html_url": "https://example.invalid/run",
                "created_at": "2026-10-26T03:00:00Z",
            }
            for conclusion in conclusions
        ]
    }


NIGHTLY = "/actions/workflows/ci.yml"
DRY_RUN = "/actions/workflows/release.yml"


def test_ci_check_passes_a_green_nightly_and_dry_run() -> None:
    """A green nightly and a green dry run at HEAD pass."""
    fetch = _fetch(
        {NIGHTLY: _runs("success"), DRY_RUN: _runs("failure", "success")}
    )
    check = release_audit.ci_check(fetch, "a" * 40)
    assert check.status == "PASS"
    assert "- Dependabot alerts: not checked" in check.lines


def test_ci_check_fails_a_red_nightly_and_a_missing_dry_run() -> None:
    """A failed nightly and no green dry run each fail."""
    fetch = _fetch({NIGHTLY: _runs("failure"), DRY_RUN: _runs()})
    check = release_audit.ci_check(fetch, "a" * 40)
    assert check.failures == [
        "the latest nightly concluded failure",
        f"no successful `release.yml` dry run at `{'a' * 8}`",
    ]


def test_ci_check_warns_on_a_cancelled_nightly() -> None:
    """A cancelled nightly proves nothing, which is not a failure."""
    fetch = _fetch({NIGHTLY: _runs("cancelled"), DRY_RUN: _runs("success")})
    assert release_audit.ci_check(fetch, "a" * 40).status == "WARN"


def test_ci_check_reports_a_rate_limit_without_passing() -> None:
    """An unreachable API never reads as a green dry run."""
    limited = GitHubError("rate limited until 04:00 UTC")
    fetch = _fetch({NIGHTLY: limited, DRY_RUN: limited})
    check = release_audit.ci_check(fetch, "a" * 40)
    assert check.status == "FAIL"
    assert "rate limited" in check.warnings[0]


def test_summary_names_every_failure() -> None:
    """The summary is READY only with no failure, and lists each one."""
    good = release_audit.Check("1. Good")
    good.warn("look at this")
    bad = release_audit.Check("2. Bad")
    bad.fail("broken")
    assert release_audit.summary([good])[0] is True
    ready, text = release_audit.summary([good, bad])
    assert ready is False
    assert "NOT READY" in text
    assert "- 2. Bad: broken" in text
