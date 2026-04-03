"""Tests for shared JSON serialization utilities."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from grelmicro.json import (
    has_orjson,
    json_default,
    json_dumps_bytes,
    json_dumps_str,
    json_loads_bytes,
)

if TYPE_CHECKING:
    from types import ModuleType

    from grelmicro.json import JSONSerializable


@pytest.fixture
def stdlib_json_module() -> ModuleType:
    """Reload _json module with orjson unavailable."""
    with patch.dict(sys.modules, {"orjson": None}):
        sys.modules.pop("grelmicro._json", None)
        module = importlib.import_module("grelmicro._json")
    try:
        return module
    finally:
        sys.modules.pop("grelmicro._json", None)
        importlib.import_module("grelmicro._json")


class TestOrjsonPath:
    """Tests for the orjson-backed implementations."""

    def test_has_orjson(self) -> None:
        """Test that has_orjson returns True when orjson is installed."""
        assert has_orjson() is True

    @pytest.mark.parametrize(
        ("obj", "expected_fragment"),
        [
            ({"key": "value"}, b'"key"'),
            ([1, 2, 3], b"[1,2,3]"),
            ("hello", b'"hello"'),
            (42, b"42"),
            (True, b"true"),
            (None, b"null"),
        ],
    )
    def test_dumps_bytes(
        self, obj: JSONSerializable, expected_fragment: bytes
    ) -> None:
        """Test json_dumps_bytes serializes various types."""
        result = json_dumps_bytes(obj)

        assert isinstance(result, bytes)
        assert expected_fragment in result

    def test_dumps_str(self) -> None:
        """Test json_dumps_str returns a string."""
        result = json_dumps_str({"key": "value"})

        assert isinstance(result, str)
        assert '"key"' in result

    def test_loads_bytes(self) -> None:
        """Test json_loads_bytes deserializes bytes."""
        result = json_loads_bytes(b'{"key":"value"}')

        assert result == {"key": "value"}

    def test_loads_str(self) -> None:
        """Test json_loads_bytes also accepts str input."""
        result = json_loads_bytes('{"key":"value"}')

        assert result == {"key": "value"}

    @pytest.mark.parametrize(
        "obj",
        [
            {"id": 42, "name": "alice", "tags": [1, 2, 3]},
            [1, "two", None, True],
            "simple string",
        ],
    )
    def test_roundtrip(self, obj: JSONSerializable) -> None:
        """Test dumps/loads roundtrip preserves data."""
        result = json_loads_bytes(json_dumps_bytes(obj))

        assert result == obj


class TestStdlibFallback:
    """Tests for the stdlib json fallback (orjson unavailable)."""

    def test_has_orjson_false(self, stdlib_json_module: ModuleType) -> None:
        """Test has_orjson returns False without orjson."""
        assert stdlib_json_module.has_orjson() is False

    def test_dumps_bytes(self, stdlib_json_module: ModuleType) -> None:
        """Test json_dumps_bytes with stdlib json."""
        result = stdlib_json_module.json_dumps_bytes({"key": "value"})

        assert isinstance(result, bytes)
        assert b'"key"' in result

    def test_dumps_str(self, stdlib_json_module: ModuleType) -> None:
        """Test json_dumps_str with stdlib json."""
        result = stdlib_json_module.json_dumps_str({"key": "value"})

        assert isinstance(result, str)
        assert '"key"' in result

    def test_loads_bytes(self, stdlib_json_module: ModuleType) -> None:
        """Test json_loads_bytes with stdlib json from bytes."""
        result = stdlib_json_module.json_loads_bytes(b'{"key":"value"}')

        assert result == {"key": "value"}

    def test_loads_str(self, stdlib_json_module: ModuleType) -> None:
        """Test json_loads_bytes with stdlib json from str."""
        result = stdlib_json_module.json_loads_bytes('{"key":"value"}')

        assert result == {"key": "value"}

    def test_roundtrip(self, stdlib_json_module: ModuleType) -> None:
        """Test stdlib dumps/loads roundtrip preserves data."""
        obj = {"id": 42, "tags": [1, 2]}

        result = stdlib_json_module.json_loads_bytes(
            stdlib_json_module.json_dumps_bytes(obj)
        )

        assert result == obj


class TestJsonDefault:
    """Tests for json_default (stdlib json fallback handler)."""

    def test_datetime_serialization(self) -> None:
        """Test datetime is serialized to ISO 8601."""
        dt = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001

        result = json_default(dt)

        assert result == "2025-01-01T12:00:00"

    def test_unsupported_type_raises(self) -> None:
        """Test non-serializable types raise TypeError."""
        with pytest.raises(TypeError, match="not JSON serializable"):
            json_default(object())


class TestDatetimeSerialization:
    """Tests for datetime handling through the full serialization path."""

    def test_dumps_bytes_with_datetime_value(self) -> None:
        """Test that a dict containing datetime is serialized correctly."""
        dt = datetime(2025, 6, 15, 10, 30, 0)  # noqa: DTZ001
        obj = {"created_at": dt, "name": "alice"}

        result = json_dumps_bytes(obj)

        assert b"2025-06-15T10:30:00" in result
        assert b"alice" in result

    def test_stdlib_dumps_bytes_with_datetime(
        self, stdlib_json_module: ModuleType
    ) -> None:
        """Test stdlib fallback handles datetime via json_default."""
        dt = datetime(2025, 6, 15, 10, 30, 0)  # noqa: DTZ001
        obj = {"created_at": dt}

        result = stdlib_json_module.json_dumps_bytes(obj)

        assert b"2025-06-15T10:30:00" in result
