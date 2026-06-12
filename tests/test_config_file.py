"""Tests for FileConfigAdapter file-format parsing and flattening."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from grelmicro.config.file import FileConfigAdapter
from grelmicro.errors import DependencyNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.timeout(5)]


async def test_json_flat_mapping(tmp_path: Path) -> None:
    """A flat .json object reads its keys and stringifies values."""
    path = tmp_path / "config.json"
    path.write_text('{"GREL_LOCK_LEDGER_LEASE_DURATION": 30, "FLAG": true}')
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {
        "GREL_LOCK_LEDGER_LEASE_DURATION": "30",
        "FLAG": "true",
    }


async def test_json_nested_mapping_flattens(tmp_path: Path) -> None:
    """A nested .json mapping joins segments with `_` and uppercases."""
    path = tmp_path / "config.json"
    path.write_text('{"grel": {"lock": {"cart": {"lease_duration": 30}}}}')
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {"GREL_LOCK_CART_LEASE_DURATION": "30"}


async def test_yaml_nested_mapping_flattens(tmp_path: Path) -> None:
    """A nested .yaml mapping flattens to GREL_... keys."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "grel:\n"
        "  lock:\n"
        "    cart:\n"
        "      lease_duration: 30\n"
        "  ratelimiter:\n"
        "    api:\n"
        "      enabled: false\n"
    )
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {
        "GREL_LOCK_CART_LEASE_DURATION": "30",
        "GREL_RATELIMITER_API_ENABLED": "false",
    }


async def test_yml_extension_reads(tmp_path: Path) -> None:
    """The .yml extension reads the same as .yaml."""
    path = tmp_path / "config.yml"
    path.write_text("GREL_RATELIMITER_API_LIMIT: 200\n")
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {"GREL_RATELIMITER_API_LIMIT": "200"}


async def test_toml_nested_mapping_flattens(tmp_path: Path) -> None:
    """A nested .toml table flattens to GREL_... keys."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[grel.lock.cart]\n"
        "lease_duration = 30\n"
        "\n"
        "[grel.ratelimiter.api]\n"
        "limit = 200\n"
        "enabled = true\n"
    )
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {
        "GREL_LOCK_CART_LEASE_DURATION": "30",
        "GREL_RATELIMITER_API_LIMIT": "200",
        "GREL_RATELIMITER_API_ENABLED": "true",
    }


async def test_toml_flat_mapping(tmp_path: Path) -> None:
    """A flat .toml mapping reads its keys directly."""
    path = tmp_path / "config.toml"
    path.write_text('GREL_RATELIMITER_API_LIMIT = "200"\n')
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {"GREL_RATELIMITER_API_LIMIT": "200"}


async def test_yaml_non_mapping_document_raises_value_error(
    tmp_path: Path,
) -> None:
    """A YAML document that is not a mapping raises ValueError."""
    path = tmp_path / "config.yaml"
    path.write_text("- 1\n- 2\n")
    adapter = FileConfigAdapter(path)
    with pytest.raises(ValueError, match="mapping of keys"):
        await adapter.load()


async def test_yaml_missing_dependency_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading YAML without PyYAML raises DependencyNotFoundError."""
    path = tmp_path / "config.yaml"
    path.write_text("GREL_LOCK_X: 1\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    adapter = FileConfigAdapter(path)
    with pytest.raises(DependencyNotFoundError, match="pyyaml"):
        await adapter.load()


async def test_env_file_still_parses(tmp_path: Path) -> None:
    """A non-mapping extension reads as .env KEY=VALUE lines."""
    path = tmp_path / "config.env"
    path.write_text(
        '# comment\n\nGREL_LOCK_X="5"\nGREL_LOCK_Y = 6\nno_equals_line\n'
    )
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {"GREL_LOCK_X": "5", "GREL_LOCK_Y": "6"}


async def test_unchanged_source_returns_none(tmp_path: Path) -> None:
    """A second load with no change returns None."""
    path = tmp_path / "config.json"
    path.write_text('{"GREL_LOCK_X": 1}')
    adapter = FileConfigAdapter(path)
    assert await adapter.load() == {"GREL_LOCK_X": "1"}
    assert await adapter.load() is None


async def test_adapter_context_manager(tmp_path: Path) -> None:
    """The adapter opens and closes as an async context manager."""
    path = tmp_path / "config.json"
    path.write_text('{"GREL_LOCK_X": 1}')
    async with FileConfigAdapter(path) as adapter:
        assert await adapter.load() == {"GREL_LOCK_X": "1"}
