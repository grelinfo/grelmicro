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


@pytest.mark.usefixtures("changelog_file")
def test_a_cut_section_is_redated_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing midnight between the cut and the merge is not a dead end.

    `release` refused any date but today and `check` demanded today, so a
    changelog PR that merged the day after the cut left no way forward:
    re-running said "pick the next version", which is wrong because nothing
    shipped. The distinction that matters is tagged, not dated.
    """
    monkeypatch.setattr(changelog, "is_tagged", lambda _version: False)
    assert changelog.release("0.40.0", "2026-08-17") == 0
    assert changelog.release("0.40.0", "2026-08-18") == 0
    assert changelog.check("0.40.0", "2026-08-18") == 0


def test_a_tagged_section_is_never_rewritten(
    changelog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag is immutable, so the section describing it is too."""
    monkeypatch.setattr(changelog, "is_tagged", lambda _version: True)
    assert changelog.release("0.40.0", "2026-08-17") == 0
    assert changelog.release("0.40.0", "2026-08-18") == 1
    assert "## 0.40.0 - 2026-08-17" in changelog_file.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("version", ["v0.40.0", "0.40", "release-1", "1.0.0.0"])
@pytest.mark.usefixtures("changelog_file")
def test_release_refuses_a_version_that_is_not_a_tag(version: str) -> None:
    """`v0.40.0` is natural to type and every tag here is bare.

    Writing it produced a heading `check` then accepted, so the mistake
    reached the printed `gh release create` line.
    """
    assert changelog.release(version, TODAY) == 1


def test_a_heading_with_trailing_space_is_still_rewritten(
    changelog_file: Path,
) -> None:
    """The writer goes through the pattern the parser matches.

    A literal string replace missed `## Unreleased ` while `sections`
    found it, so the cut reported success and changed nothing, and the
    failure surfaced two steps later as "no section for 0.40.0".
    """
    changelog_file.write_text(
        SAMPLE.replace("## Unreleased", "## Unreleased "), encoding="utf-8"
    )
    assert changelog.release("0.40.0", TODAY) == 0
    assert changelog.check("0.40.0", TODAY) == 0


def test_a_section_of_only_headings_is_not_entries(
    changelog_file: Path,
) -> None:
    """A release body that is a bare sub-heading describes nothing."""
    changelog_file.write_text(
        "# Changelog\n\n## Unreleased\n\n### Added\n\n## 0.39.0 - 2026-08-16\n\n* ✨ Old.\n",
        encoding="utf-8",
    )
    assert changelog.release("0.40.0", TODAY) == 1


def test_entries_added_after_the_cut_are_reported(
    changelog_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They ship inside the tag while the notes say nothing about them."""
    monkeypatch.setattr(changelog, "is_tagged", lambda _version: False)
    assert changelog.release("0.40.0", TODAY) == 0
    text = changelog_file.read_text(encoding="utf-8")
    changelog_file.write_text(
        text.replace(
            f"## 0.40.0 - {TODAY}",
            "## Unreleased\n\n* ✨ Merged after the cut.\n\n## 0.40.0 - "
            + TODAY,
        ),
        encoding="utf-8",
    )
    assert changelog.release("0.40.0", TODAY) == 0
    assert "added under 'Unreleased' after the cut" in capsys.readouterr().err
