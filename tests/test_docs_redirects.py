"""Check that every redirect of a moved documentation page lands on a page.

A page that moves or goes away keeps its old URL alive through a redirect in
`mkdocs.yml`, so search engines and saved links keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent

_DOCS = _ROOT / "docs"


class _Loader(yaml.SafeLoader):
    """YAML loader that ignores the `!!python/name:` tags in `mkdocs.yml`."""


_Loader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda *_: None,
)


def _redirects() -> dict[str, str]:
    """Return the `redirect_maps` of the redirects plugin in `mkdocs.yml`."""
    config = yaml.load(
        (_ROOT / "mkdocs.yml").read_text(encoding="utf-8"),
        Loader=_Loader,  # noqa: S506 - a SafeLoader that drops one tag
    )
    for plugin in config["plugins"]:
        if isinstance(plugin, dict) and "redirects" in plugin:
            return plugin["redirects"]["redirect_maps"]
    return {}


_REDIRECTS = sorted(_redirects().items())


def test_redirects_are_configured() -> None:
    """The redirects plugin lists the moved pages."""
    assert _REDIRECTS


@pytest.mark.parametrize(("old", "new"), _REDIRECTS)
def test_redirect_lands_on_a_page(old: str, new: str) -> None:
    """The target exists, and the old path holds no page the redirect would hide."""
    assert (_DOCS / new).is_file(), f"{old} redirects to a missing page {new}"
    assert not (_DOCS / old).exists(), (
        f"{old} is a page again, drop its redirect"
    )
