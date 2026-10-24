"""Tests for the warnings that announce a behavior change or a rename."""

import warnings
from dataclasses import replace

import pytest

import grelmicro
from grelmicro._deprecation import PENDING, FutureChange, Removal, Rename
from grelmicro.errors import GrelmicroConfigWarning, GrelmicroFutureWarning
from tests._future import assert_future_warning, latest_release, stale
from tools import changelog

GATE = FutureChange(
    change="`Example(gate=)` defaults to `leader`",
    switch_in="0.44.0",
    keep="gate=None",
    adopt="gate=leader",
)

OLD_NAME = Rename(old="OldName", new="NewName", removed_in="0.44.0")

OLD_ENUM = Removal(
    old="OldEnum", use='`"value"` and the `New` type', removed_in="0.44.0"
)


def _example(gate: object = None, *, explicit: bool = False) -> object:
    """Stand in for a library call that warns while `gate` is left at its default."""
    if not explicit:
        GATE.warn()
    return gate


def test_category_is_a_future_warning_of_its_own() -> None:
    """End users see it by default, and config filters do not silence it."""
    assert issubclass(GrelmicroFutureWarning, FutureWarning)
    assert not issubclass(GrelmicroFutureWarning, GrelmicroConfigWarning)
    assert grelmicro.GrelmicroFutureWarning is GrelmicroFutureWarning


def test_future_change_message() -> None:
    """The message says what changes, when, and the argument for each choice."""
    assert GATE.message == (
        "`Example(gate=)` defaults to `leader` from 0.44.0. "
        "Pass `gate=None` to keep the current behavior, "
        "or `gate=leader` to adopt the new one now."
    )


def test_future_change_message_for_an_environment_variable() -> None:
    """An environment variable is set, not passed."""
    entry = replace(
        GATE, keep="GREL_GATE=none", adopt="GREL_GATE=leader", verb="Set"
    )

    assert entry.message == (
        "`Example(gate=)` defaults to `leader` from 0.44.0. "
        "Set `GREL_GATE=none` to keep the current behavior, "
        "or `GREL_GATE=leader` to adopt the new one now."
    )


def test_future_change_message_with_nothing_to_keep() -> None:
    """With no `keep`, the message offers only the new behavior."""
    entry = replace(GATE, keep=None)

    assert entry.message == (
        "`Example(gate=)` defaults to `leader` from 0.44.0. "
        "Pass `gate=leader` now."
    )


def test_future_change_warns_under_its_category() -> None:
    """An entry with a narrower category warns under it, at the caller."""

    class NarrowWarning(GrelmicroFutureWarning):
        """A narrower future warning."""

    def example() -> None:
        replace(GATE, category=NarrowWarning).warn()

    with pytest.warns(NarrowWarning) as caught:
        example()
    assert caught[0].filename == __file__


def test_rename_message() -> None:
    """The message names the new name and the release that removes the old one."""
    assert OLD_NAME.message == (
        "`OldName` is renamed to `NewName`. `OldName` is removed in 0.44.0."
    )


def test_removal_message() -> None:
    """The message names the release that removes the name and what replaces it."""
    assert OLD_ENUM.message == (
        "`OldEnum` is removed in 0.44.0. "
        'Use `"value"` and the `New` type instead.'
    )


def test_removal_warns_with_its_message() -> None:
    """A removal warns under the same category, at the caller."""

    def old_enum() -> None:
        OLD_ENUM.warn()

    with pytest.warns(
        GrelmicroFutureWarning, match="removed in 0.44.0"
    ) as caught:
        old_enum()
    assert caught[0].filename == __file__


def test_warning_points_at_the_caller_of_the_library() -> None:
    """The reported line is the user's call, not grelmicro's."""
    with pytest.warns(GrelmicroFutureWarning) as caught:
        _example()
    assert caught[0].filename == __file__


def test_rename_warns_with_its_message() -> None:
    """A rename warns under the same category."""

    def old_name() -> None:
        OLD_NAME.warn()

    with pytest.warns(
        GrelmicroFutureWarning, match="removed in 0.44.0"
    ) as caught:
        old_name()
    assert caught[0].filename == __file__


def test_assert_future_warning_accepts_a_correct_pair() -> None:
    """The default call warns and the explicit one stays silent."""
    assert_future_warning(
        GATE, default=_example, explicit=lambda: _example(explicit=True)
    )


def test_assert_future_warning_fails_when_explicit_warns() -> None:
    """Passing the argument must silence the warning."""
    with pytest.raises(AssertionError):
        assert_future_warning(GATE, default=_example, explicit=_example)


def test_assert_future_warning_fails_when_default_is_silent() -> None:
    """The default must warn."""
    with pytest.raises(pytest.fail.Exception):
        assert_future_warning(
            GATE,
            default=lambda: _example(explicit=True),
            explicit=lambda: _example(explicit=True),
        )


def test_assert_future_warning_ignores_other_warnings() -> None:
    """Only a `GrelmicroFutureWarning` from the explicit call fails it."""

    def explicit() -> None:
        warnings.warn("unrelated", UserWarning, stacklevel=1)

    assert_future_warning(GATE, default=_example, explicit=explicit)


def test_no_pending_entry_outlives_its_release() -> None:
    """A warning or an alias cannot ship in the release that should drop it.

    Switch the default or remove the old name, then delete the entry.
    """
    current = latest_release(changelog.CHANGELOG.read_text(encoding="utf-8"))
    assert stale(PENDING, current) == []


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.43.9", []),
        ("0.44.0", [OLD_NAME, OLD_ENUM, GATE]),
        ("1.0.0", [OLD_NAME, OLD_ENUM, GATE]),
    ],
)
def test_stale_flags_entries_due_at_or_before_the_version(
    version: str, expected: list[FutureChange | Rename | Removal]
) -> None:
    """The guard fails from the release named on the entry onward."""
    assert stale([OLD_NAME, OLD_ENUM, GATE], version) == expected


def test_stale_refuses_a_version_it_cannot_compare() -> None:
    """A pre-release or a short version is a mistake on the entry."""
    with pytest.raises(AssertionError, match=r"MAJOR\.MINOR\.PATCH"):
        stale([Rename(old="a", new="b", removed_in="0.44")], "0.44.0")


def test_latest_release_skips_unreleased() -> None:
    """The guard compares against the newest dated section."""
    text = "# Changelog\n\n## Unreleased\n\n## 0.43.0 - 2026-11-01\n\n## 0.42.0 - 2026-10-01\n"
    assert latest_release(text) == "0.43.0"


def test_latest_release_needs_a_dated_section() -> None:
    """A changelog with nothing released yet cannot anchor the guard."""
    with pytest.raises(AssertionError, match="no dated release"):
        latest_release("# Changelog\n\n## Unreleased\n")
