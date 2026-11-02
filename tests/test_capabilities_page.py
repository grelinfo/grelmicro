"""Check that the capability matrix agrees with what ships.

The levels are written by hand. These checks catch a Pattern a provider builds
that the page calls missing, a cell the page rates that nothing builds, a cell
whose hover text names another level, a feature missing from the last-changes
table, and a version the changelog does not have. Date logic
stays out on purpose, so a test never fails because a day passed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from grelmicro.coordination import kubernetes
from grelmicro.providers._base import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.providers.valkey import ValkeyProvider

_PAGE = Path(__file__).resolve().parent.parent / "docs" / "capabilities.md"

_LEVELS = {
    "🚧": "Planned",
    "🧪": "Experimental",
    "🔍": "Preview",
    "✅": "Stable",
    "✅⚠️": "Stable, changing",
    "➖": "Does not apply",  # noqa: RUF001
}
"""Every icon a table cell may hold, with the hover text it must carry."""

_NOT_SHIPPED = {"🚧", "➖"}  # noqa: RUF001
"""The icons that say a combination does not ship."""

_FACTORIES = {
    "Lock": "lock_backend",
    "TaskLock": "lock_backend",
    "Idempotency": "cache_backend",
    "ReadWriteLock": "readwritelock_backend",
    "LeaderElection": "leaderelection_backend",
    "@tasks.cron": "schedule_backend",
    "TTLCache": "cache_backend",
    "Outbox": "outbox_backend",
    "RateLimiter": "ratelimiter_backend",
    "CircuitBreaker": "circuitbreaker_backend",
}
"""Each matrix row a provider builds, with the factory that builds it."""

_PROVIDERS = {
    "Memory": MemoryProvider,
    "Redis": RedisProvider,
    "Redis Sentinel": RedisProvider,
    "Redis Cluster": RedisProvider,
    "Valkey": ValkeyProvider,
    "Postgres": PostgresProvider,
    "SQLite": SQLiteProvider,
}
"""Each matrix column backed by a provider."""

_KUBERNETES_ADAPTERS = {
    "Lock": "KubernetesLockAdapter",
    "TaskLock": "KubernetesLockAdapter",
    "ReadWriteLock": "KubernetesReadWriteLockAdapter",
    "LeaderElection": "KubernetesLeaderElectionAdapter",
}
"""The matrix rows Kubernetes builds, with no provider in front of them."""

_MIN_ICON_CELLS = 80
"""Floor for the sweep, so a page that lost its cells cannot pass silently."""

_CELL = re.compile(
    r'<span role="img" title="([^"]+)" aria-label="([^"]+)">([^<]+)</span>'
)


def _icon(cell: str) -> str:
    """Return the icon of one table cell, failing on a cell with no hover text."""
    match = _CELL.fullmatch(cell)
    assert match is not None, cell
    return match.group(3)


def _section(heading: str) -> str:
    """Return the page text under one `##` heading."""
    text = _PAGE.read_text(encoding="utf-8")
    start = text.index(f"## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end]


def _matrix() -> dict[str, dict[str, str]]:
    """Return the icon of each matrix cell, by Pattern, then by column."""
    lines = [
        line
        for line in _section("Patterns on a backend").splitlines()
        if line.startswith("|")
    ]
    columns = [cell.strip() for cell in lines[0].strip("|").split("|")][1:]
    matrix: dict[str, dict[str, str]] = {}
    for line in lines[2:]:
        name, *cells = [cell.strip() for cell in line.strip("|").split("|")]
        icons = [_icon(cell) for cell in cells]
        matrix[name.strip("`")] = dict(zip(columns, icons, strict=True))
    return matrix


def _level_features() -> set[str]:
    """Return every feature named in the matrix and the no-backend table."""
    features = set(_matrix())
    for row in re.findall(
        r"<tr><td>(.+?)</td>", _section("Features with no backend")
    ):
        features.add(re.sub(r"</?code>", "`", row).strip("`"))
    return features


def _dated_features() -> set[str]:
    """Return every feature named in the collapsed dates table."""
    rows = [
        line.strip()
        for line in _section("Last changes").splitlines()
        if line.strip().startswith("|")
    ]
    names = {row.strip("|").split("|")[0].strip() for row in rows[2:]}
    return {name.strip("`") for name in names} - {"Whole library"}


@pytest.mark.parametrize("pattern", _FACTORIES)
@pytest.mark.parametrize("column", _PROVIDERS)
def test_a_cell_ships_when_its_provider_builds_it(
    pattern: str, column: str
) -> None:
    """A provider factory and the page agree on what ships."""
    factory = _FACTORIES[pattern]
    builds = getattr(_PROVIDERS[column], factory) is not getattr(
        Provider, factory
    )
    assert (_matrix()[pattern][column] not in _NOT_SHIPPED) is builds


@pytest.mark.parametrize("pattern", _FACTORIES)
def test_a_kubernetes_cell_ships_when_its_adapter_exists(pattern: str) -> None:
    """The Kubernetes module and the page agree on what ships."""
    adapter = _KUBERNETES_ADAPTERS.get(pattern)
    builds = adapter is not None and hasattr(kubernetes, adapter)
    assert (_matrix()[pattern]["Kubernetes"] not in _NOT_SHIPPED) is builds


def test_every_icon_carries_the_hover_text_of_its_level() -> None:
    """A reader who hovers an icon sees the level the icon stands for."""
    cells = _CELL.findall(_PAGE.read_text(encoding="utf-8"))
    assert len(cells) > _MIN_ICON_CELLS, "the page lost its icon cells"
    for title, label, icon in cells:
        assert _LEVELS[icon] == title == label


def test_every_feature_with_a_level_has_dates_and_the_reverse() -> None:
    """The level tables and the dates table name the same features."""
    assert _level_features() == _dated_features()


def test_every_no_backend_level_is_an_icon_with_hover_text() -> None:
    """A level in the no-backend table carries the same markup as the matrix."""
    levels = re.findall(
        r"<tr><td>.+?</td><td[^>]*>(.+?)</td></tr>",
        _section("Features with no backend"),
    )
    assert levels, "the no-backend table lost its rows"
    for level in levels:
        assert _LEVELS[_icon(level)]


def test_every_version_in_the_dates_table_is_in_the_changelog() -> None:
    """A mistyped version cannot sit in the last-changes table."""
    changelog = _PAGE.with_name("changelog.md").read_text(encoding="utf-8")
    released = set(re.findall(r"^## (\d+\.\d+\.\d+) ", changelog, re.MULTILINE))
    rows = [
        line.strip()
        for line in _section("Last changes").splitlines()
        if line.strip().startswith("|")
    ]
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert {cells[1], cells[2]} - {"None"} <= released, row
