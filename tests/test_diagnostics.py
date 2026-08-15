"""Tests for the startup diagnostic codes and their silencing."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

import grelmicro._diagnostics as diagnostics
from grelmicro.errors import (
    AmbientBindingWarning,
    BackendScopeError,
    BackendScopeWarning,
    EnvLoadOffWarning,
    GrelmicroConfigWarning,
    SentinelPasswordWarning,
    UnknownEnvironmentWarning,
)

_CATEGORIES: list[type[GrelmicroConfigWarning]] = [
    EnvLoadOffWarning,
    UnknownEnvironmentWarning,
    BackendScopeWarning,
    AmbientBindingWarning,
    SentinelPasswordWarning,
]

_DOCS = Path(__file__).parent.parent / "docs" / "diagnostics.md"


def _codes() -> set[str]:
    """Return every declared diagnostic code."""
    return {
        value
        for name, value in vars(diagnostics).items()
        if name.isupper() and isinstance(value, str) and name != "DOCS_URL"
    }


def test_code_is_a_kebab_case_slug() -> None:
    """A code names the problem, so it never needs a lookup table."""
    for code in _codes():
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", code), code


def test_every_code_has_a_docs_section() -> None:
    """The rendered URL points at a heading that exists.

    The heading is the code, so the anchor is exactly as stable as the code
    itself. This test is what keeps that true.
    """
    text = _DOCS.read_text()

    for code in _codes():
        assert f"### `{code}`" in text, code


@pytest.mark.parametrize("category", _CATEGORIES)
def test_category_carries_its_code(
    category: type[GrelmicroConfigWarning],
) -> None:
    """Every warning category exposes the code as a structured attribute."""
    assert category.code in _codes()


@pytest.mark.parametrize("category", _CATEGORIES)
def test_category_derives_from_the_base(
    category: type[GrelmicroConfigWarning],
) -> None:
    """One filter on the base category still silences every diagnostic."""
    assert issubclass(category, GrelmicroConfigWarning)


def test_message_trails_the_code_and_the_link() -> None:
    """The reader meets the problem before the label."""
    message = diagnostics.diagnostic(diagnostics.BACKEND_SCOPE, "Something.")

    assert message.startswith("Something.")
    assert "[backend-scope]" in message
    assert message.endswith("/diagnostics/#backend-scope")


def test_one_diagnostic_silences_without_silencing_the_rest() -> None:
    """Filtering is by category, never by matching the message text."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=EnvLoadOffWarning)
        warnings.warn("a", EnvLoadOffWarning, stacklevel=1)
        warnings.warn("b", BackendScopeWarning, stacklevel=1)

    assert [w.category for w in caught] == [BackendScopeWarning]


def test_base_category_silences_every_diagnostic() -> None:
    """The promise `GrelmicroConfigWarning` already documents still holds."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=GrelmicroConfigWarning)
        warnings.warn("a", EnvLoadOffWarning, stacklevel=1)
        warnings.warn("b", BackendScopeWarning, stacklevel=1)

    assert caught == []


def test_warning_and_error_share_one_code() -> None:
    """Severity is not part of the identity.

    An unmet backend scope is a warning with no tier declared and an error in
    `staging` and `production`. One problem, one code, two reports.
    """
    assert BackendScopeWarning.code == BackendScopeError.code
