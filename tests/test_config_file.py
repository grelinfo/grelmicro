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
    # Every reading a field could want. Walking the mapping names the
    # scalars, and each level names itself as JSON for a field that takes
    # a mapping. A key naming no field matches nothing and is ignored.
    assert await adapter.load() == {
        "GREL": '{"lock":{"cart":{"lease_duration":30}}}',
        "GREL_LOCK": '{"cart":{"lease_duration":30}}',
        "GREL_LOCK_CART": '{"lease_duration":30}',
        "GREL_LOCK_CART_LEASE_DURATION": "30",
    }


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
        "GREL": (
            '{"lock":{"cart":{"lease_duration":30}},'
            '"ratelimiter":{"api":{"enabled":false}}}'
        ),
        "GREL_LOCK": '{"cart":{"lease_duration":30}}',
        "GREL_LOCK_CART": '{"lease_duration":30}',
        "GREL_LOCK_CART_LEASE_DURATION": "30",
        "GREL_RATELIMITER": '{"api":{"enabled":false}}',
        "GREL_RATELIMITER_API": '{"enabled":false}',
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
    loaded = await adapter.load()
    assert loaded is not None
    assert loaded["GREL_LOCK_CART_LEASE_DURATION"] == "30"
    assert loaded["GREL_RATELIMITER_API_LIMIT"] == "200"
    assert loaded["GREL_RATELIMITER_API_ENABLED"] == "true"
    assert loaded["GREL_RATELIMITER_API"] == ('{"limit":200,"enabled":true}')


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


async def test_an_instance_name_reaches_the_prefix_it_reads(
    tmp_path: Path,
) -> None:
    """A name is normalised into a segment, in the document as in code.

    `Lock("cart.v2")` reads `GREL_LOCK_CART_V2_*`, so a document that
    writes the name as it was given has to arrive there. Deciding by the
    shape of the key instead would make the dot end the nesting and take
    every sibling down with it.
    """
    # Arrange
    path = tmp_path / "config.yaml"
    path.write_text(
        "grel:\n"
        "  lock:\n"
        "    lease_duration: 30\n"
        "    cart.v2:\n"
        "      lease_duration: 60\n"
    )
    adapter = FileConfigAdapter(path)

    # Act
    loaded = await adapter.load()

    # Assert
    assert loaded is not None
    assert loaded["GREL_LOCK_LEASE_DURATION"] == "30"
    assert loaded["GREL_LOCK_CART_V2_LEASE_DURATION"] == "60"


async def test_a_field_holding_a_mapping_reads_it_whole(
    tmp_path: Path,
) -> None:
    """A mapping is a value here and a level of nesting there.

    `headers` is one field's value and its keys look like segments.
    `include` is one field's value and its keys cannot be segments at
    all. Both readings are written, so whichever names the field is the
    one that fills it.
    """
    # Arrange
    path = tmp_path / "config.yaml"
    path.write_text(
        "grel:\n"
        "  metrics:\n"
        '    headers: {"authorization": "Bearer x"}\n'
        "  cached_responses:\n"
        '    include: {"/products/*": 60}\n'
    )
    adapter = FileConfigAdapter(path)

    # Act
    loaded = await adapter.load()

    # Assert
    assert loaded is not None
    assert loaded["GREL_METRICS_HEADERS"] == '{"authorization":"Bearer x"}'
    assert loaded["GREL_CACHED_RESPONSES_INCLUDE"] == '{"/products/*":60}'


async def test_a_key_no_variable_can_be_named_from_is_skipped(
    tmp_path: Path,
) -> None:
    """A key no segment can be built from ends the walk under it.

    Nothing below it is reachable by name, so writing those names would
    only add keys that match nothing. The mapping above already wrote
    itself as JSON, which is the reading a field holding it takes.
    """
    # Arrange
    path = tmp_path / "config.yaml"
    path.write_text(
        "grel:\n"
        "  cached_responses:\n"
        "    include:\n"
        '      "***":\n'
        "        nested: 1\n"
    )
    adapter = FileConfigAdapter(path)

    # Act
    loaded = await adapter.load()

    # Assert
    assert loaded is not None
    assert loaded["GREL_CACHED_RESPONSES_INCLUDE"] == '{"***":{"nested":1}}'
    assert not any("NESTED" in key for key in loaded)


async def test_a_value_json_has_no_form_for_is_still_walked(
    tmp_path: Path,
) -> None:
    """One reading being unavailable does not cost the document the other.

    A YAML document holds a binary scalar, and JSON has no form for one,
    so that mapping is not offered as a value. Its scalars still reach
    the names they were written under.
    """
    # Arrange
    path = tmp_path / "config.yaml"
    path.write_text(
        "grel:\n  outbox:\n    seed: !!binary aGk=\n    batch_size: 10\n"
    )
    adapter = FileConfigAdapter(path)

    # Act
    loaded = await adapter.load()

    # Assert
    assert loaded is not None
    assert loaded["GREL_OUTBOX_BATCH_SIZE"] == "10"
    assert "GREL_OUTBOX" not in loaded


async def test_a_list_json_has_no_form_for_is_still_written(
    tmp_path: Path,
) -> None:
    """A key nothing may even read must not take the document down.

    Raising here would fail every poll over one value, and
    `ExternalConfig` would log it and keep the last good config, so the
    whole mounted file would go quiet.
    """
    # Arrange
    path = tmp_path / "config.yaml"
    path.write_text("GREL_OUTBOX_SEEDS:\n  - !!binary aGk=\n")
    adapter = FileConfigAdapter(path)

    # Act
    loaded = await adapter.load()

    # Assert
    assert loaded is not None
    assert "GREL_OUTBOX_SEEDS" in loaded
