"""Tests for cache serializers."""

from __future__ import annotations

from typing import TypedDict

import pytest
from pydantic import BaseModel

from grelmicro.cache.serializers import (
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
)

pytestmark = [pytest.mark.timeout(10)]


EXPECTED_USER_ID = 42
EXPECTED_PRICE = 9.99


class _User(BaseModel):
    id: int
    name: str


class TestPickleSerializer:
    """Tests for PickleSerializer."""

    def test_roundtrip_dict(self) -> None:
        """Test pickle roundtrip with a dict."""
        serializer = PickleSerializer()
        obj = {"id": 1, "name": "Alice"}

        result = serializer.loads(serializer.dumps(obj))

        assert result == obj

    def test_roundtrip_pydantic(self) -> None:
        """Test pickle roundtrip with a Pydantic model."""
        serializer = PickleSerializer()
        user = _User(id=1, name="Alice")

        result = serializer.loads(serializer.dumps(user))

        assert isinstance(result, _User)
        assert result == user

    def test_custom_protocol(self) -> None:
        """Test pickle with a specific protocol version."""
        serializer = PickleSerializer(protocol=4)

        data = serializer.dumps({"key": "value"})
        result = serializer.loads(data)

        assert result == {"key": "value"}


class TestJsonSerializer:
    """Tests for JsonSerializer."""

    def test_roundtrip_dict(self) -> None:
        """Test JSON roundtrip with a dict."""
        serializer = JsonSerializer()
        obj = {"id": 1, "tags": ["a", "b"]}

        result = serializer.loads(serializer.dumps(obj))

        assert result == obj

    def test_roundtrip_list(self) -> None:
        """Test JSON roundtrip with a list."""
        serializer = JsonSerializer()
        obj = [1, "two", None, True]

        result = serializer.loads(serializer.dumps(obj))

        assert result == obj

    def test_output_is_bytes(self) -> None:
        """Test that dumps returns bytes."""
        serializer = JsonSerializer()

        result = serializer.dumps({"key": "value"})

        assert isinstance(result, bytes)


class TestPydanticSerializer:
    """Tests for PydanticSerializer."""

    def test_roundtrip_model(self) -> None:
        """Test Pydantic roundtrip preserves model type."""
        serializer = PydanticSerializer(_User)
        user = _User(id=EXPECTED_USER_ID, name="Bob")

        data = serializer.dumps(user)
        result = serializer.loads(data)

        assert isinstance(result, _User)
        assert result.id == EXPECTED_USER_ID
        assert result.name == "Bob"

    def test_output_is_json_bytes(self) -> None:
        """Test that dumps produces JSON bytes."""
        serializer = PydanticSerializer(_User)
        user = _User(id=1, name="Alice")

        data = serializer.dumps(user)

        assert isinstance(data, bytes)
        assert b'"id"' in data

    def test_validates_on_load(self) -> None:
        """Test that loads validates the data against the model."""
        serializer = PydanticSerializer(_User)

        result = serializer.loads(b'{"id": "42", "name": "Alice"}')

        assert result.id == EXPECTED_USER_ID  # coerced from string

    def test_works_with_typed_dict(self) -> None:
        """Test PydanticSerializer with TypedDict."""

        class Item(TypedDict):
            sku: str
            price: float

        serializer = PydanticSerializer(Item)
        item = Item(sku="ABC", price=9.99)

        result = serializer.loads(serializer.dumps(item))

        assert result == item
