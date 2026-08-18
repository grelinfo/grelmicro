"""The changelog writer produces what the changelog checker demands.

`release` renames the `Unreleased` heading and `check` gates the tag on
that rename having happened. They share one parser so they cannot disagree
about the format, and this holds them to it: whatever the writer emits, the
checker accepts.

The rename was the one manual step in the release procedure, and a release
tag is immutable, so getting it wrong costs a version.
"""

import datetime
from pathlib import Path

import pytest

from tools import changelog

TODAY = datetime.datetime.now(tz=datetime.UTC).date().isoformat()

SAMPLE = """# Changelog

## Unreleased

### Breaking

* 💥 Something that breaks.

## 0.39.0 - 2026-08-16

### Added

* ✨ Something older.
"""


@pytest.fixture
def changelog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tool at a throwaway changelog."""
    path = tmp_path / "changelog.md"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", path)
    return path


def test_release_then_check_agree(changelog_file: Path) -> None:
    """Whatever the writer emits, the checker accepts.

    This is the reason both live in one tool. A separate writer could
    format a heading the checker rejects, and the failure would land at
    release time.
    """
    assert changelog.release("0.40.0", TODAY) == 0
    assert f"## 0.40.0 - {TODAY}" in changelog_file.read_text(encoding="utf-8")
    assert changelog.check("0.40.0", TODAY) == 0


def test_release_is_idempotent(changelog_file: Path) -> None:
    """Re-running after a later step failed does not corrupt the file."""
    assert changelog.release("0.40.0", TODAY) == 0
    first = changelog_file.read_text(encoding="utf-8")
    assert changelog.release("0.40.0", TODAY) == 0
    assert changelog_file.read_text(encoding="utf-8") == first


def test_release_refuses_to_rewrite_a_released_section(
    changelog_file: Path,
) -> None:
    """A version already cut is never rewritten.

    A tag is immutable, so its changelog section is too. Rewriting one
    would describe a release that shipped something else.
    """
    assert changelog.release("0.39.0", TODAY) == 1
    assert "## 0.39.0 - 2026-08-16" in changelog_file.read_text(
        encoding="utf-8"
    )


def test_release_refuses_without_an_unreleased_section(
    changelog_file: Path,
) -> None:
    """Nothing to cut is a failure, not a silent success."""
    changelog_file.write_text(
        SAMPLE.replace(
            "## Unreleased\n\n### Breaking\n\n* 💥 Something that breaks.\n\n",
            "",
        ),
        encoding="utf-8",
    )
    assert changelog.release("0.40.0", TODAY) == 1


def test_release_refuses_an_empty_unreleased_section(
    changelog_file: Path,
) -> None:
    """An empty section would cut a release describing nothing."""
    changelog_file.write_text(
        "# Changelog\n\n## Unreleased\n\n## 0.39.0 - 2026-08-16\n\n* ✨ Old.\n",
        encoding="utf-8",
    )
    assert changelog.release("0.40.0", TODAY) == 1


@pytest.mark.usefixtures("changelog_file")
def test_check_rejects_a_changelog_still_saying_unreleased() -> None:
    """The gate that makes the rename impossible to forget."""
    assert changelog.check("0.40.0", TODAY) == 1


@pytest.mark.usefixtures("changelog_file")
def test_notes_returns_the_body_that_becomes_the_release() -> None:
    """The GitHub Release body is the section, so it must survive the cut."""
    assert changelog.release("0.40.0", TODAY) == 0
    assert changelog.notes("0.40.0") == 0
