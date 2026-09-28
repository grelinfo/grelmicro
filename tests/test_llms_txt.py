"""Check that every documentation page is indexed for LLM readers.

`docs/llms.txt` is the curated index an agent reads first, kept inside a
token budget. `docs/llms-full.txt` lists every page. A page missing from
both is a page an agent concludes does not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent

_SITE_URL = "https://grelmicro.grel.info"
"""Base URL of the published documentation site."""

_INDEXES = ("docs/llms.txt", "docs/llms-full.txt")
"""The indexes a nav page may be listed in."""

_MAX_CURATED_BYTES = 8_000
"""Size ceiling for `docs/llms.txt`.

The llms.txt specification exists to fit a context window, so the curated
index stays small enough to read whole. Completeness belongs in
`docs/llms-full.txt`.
"""


class _NavLoader(yaml.SafeLoader):
    """YAML loader that ignores the `!!python/name:` tags in `mkdocs.yml`."""


_NavLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda *_: None,
)


def _nav_pages() -> list[str]:
    """Return every Markdown page listed in the MkDocs nav."""
    config = yaml.load(
        (_ROOT / "mkdocs.yml").read_text(encoding="utf-8"),
        Loader=_NavLoader,  # noqa: S506 - a SafeLoader that drops one tag
    )
    pages: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            pages.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(config["nav"])
    return pages


def _page_url(page: str) -> str:
    """Return the published URL of a nav page."""
    path = page.removesuffix(".md")
    if path == "index":
        return f"{_SITE_URL}/"
    path = path.removesuffix("/index")
    return f"{_SITE_URL}/{path}/"


def _index_text() -> str:
    """Return both indexes as one string."""
    return "\n".join(
        (_ROOT / name).read_text(encoding="utf-8") for name in _INDEXES
    )


@pytest.mark.parametrize("page", _nav_pages())
def test_llms_index_links_every_nav_page(page: str) -> None:
    """Every page in the MkDocs nav is linked from an llms index."""
    # Arrange
    target = f"]({_page_url(page)})"
    # Act
    text = _index_text()
    # Assert
    assert target in text, f"{page} is in the nav but in no llms index"


def test_llms_txt_stays_within_its_token_budget() -> None:
    """The curated index stays small enough for an agent to read whole."""
    # Arrange
    curated = _ROOT / "docs" / "llms.txt"
    # Act
    size = curated.stat().st_size
    # Assert
    assert size <= _MAX_CURATED_BYTES


def test_llms_txt_points_at_the_full_index() -> None:
    """The curated index tells a reader where the rest of the pages are."""
    # Arrange
    curated = (_ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")
    # Act
    link = f"{_SITE_URL}/llms-full.txt"
    # Assert
    assert link in curated
