"""Tests for shared JSON serialization utilities."""

import importlib
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

from grelmicro._json import (
    _json_default,
    has_orjson,
    json_dumps_bytes,
    json_dumps_str,
)


def test_json_dumps_bytes_with_orjson() -> None:
    """Test that json_dumps_bytes uses orjson when available."""
    result = json_dumps_bytes({"key": "value"})

    assert isinstance(result, bytes)
    assert b'"key"' in result
    assert b'"value"' in result


def test_json_dumps_str_with_orjson() -> None:
    """Test that json_dumps_str uses orjson when available."""
    result = json_dumps_str({"key": "value"})

    assert isinstance(result, str)
    assert '"key"' in result
    assert '"value"' in result


def test_has_orjson_returns_true() -> None:
    """Test that has_orjson returns True when orjson is installed."""
    assert has_orjson() is True


def _reload_without_orjson():  # noqa: ANN202
    """Reload _json module with orjson unavailable."""
    with patch.dict(sys.modules, {"orjson": None}):
        if "grelmicro._json" in sys.modules:
            del sys.modules["grelmicro._json"]
        module = importlib.import_module("grelmicro._json")
    # Restore original module after test
    if "grelmicro._json" in sys.modules:
        del sys.modules["grelmicro._json"]
    importlib.import_module("grelmicro._json")
    return module


def test_json_dumps_bytes_stdlib_fallback() -> None:
    """Test json_dumps_bytes falls back to stdlib json."""
    module = _reload_without_orjson()

    result = module.json_dumps_bytes({"key": "value"})

    assert isinstance(result, bytes)
    assert b'"key"' in result


def test_json_dumps_str_stdlib_fallback() -> None:
    """Test json_dumps_str falls back to stdlib json."""
    module = _reload_without_orjson()

    result = module.json_dumps_str({"key": "value"})

    assert isinstance(result, str)
    assert '"key"' in result


def test_has_orjson_false_without_orjson() -> None:
    """Test has_orjson returns False when orjson is not available."""
    module = _reload_without_orjson()

    assert module.has_orjson() is False


def test_json_default_with_datetime() -> None:
    """Test _json_default handles datetime objects."""
    dt = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001

    assert _json_default(dt) == "2025-01-01T12:00:00"


def test_json_default_with_unsupported_type() -> None:
    """Test _json_default raises TypeError for unsupported types."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        _json_default(object())
