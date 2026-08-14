"""Tests for cache serializers."""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict, TypeVar

from grelmicro.cache.serializers import (
    CacheSerializer,
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
    _infer_serializer,
    _infer_serializer_from_class,
    _infer_serializer_from_instance,
    _resolve_serializer,
)

pytestmark = [pytest.mark.timeout(10)]


EXPECTED_USER_ID = 42

T = TypeVar("T")


class _User(BaseModel):
    id: int
    name: str


class TestCacheSerializerProtocol:
    """Tests for CacheSerializer protocol conformance."""

    def test_pickle_serializer_satisfies_protocol(self) -> None:
        """Test PickleSerializer is a CacheSerializer."""
        assert isinstance(PickleSerializer(), CacheSerializer)

    def test_json_serializer_satisfies_protocol(self) -> None:
        """Test JsonSerializer is a CacheSerializer."""
        assert isinstance(JsonSerializer(), CacheSerializer)

    def test_pydantic_serializer_satisfies_protocol(self) -> None:
        """Test PydanticSerializer is a CacheSerializer."""
        assert isinstance(PydanticSerializer(_User), CacheSerializer)


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

    def test_protocol_is_stored_and_used_for_dumps(self) -> None:
        """`dumps` uses the configured protocol, not the pickle default.

        The bytes from a non-default protocol must match
        the standard library output for that protocol exactly, so dropping
        the protocol kwarg (which falls back to the default) is caught.
        """
        non_default_protocol = 2
        obj = {"key": "value"}
        serializer = PickleSerializer(protocol=non_default_protocol)

        assert serializer._protocol == non_default_protocol
        assert serializer.dumps(obj) == pickle.dumps(
            obj, protocol=non_default_protocol
        )
        assert serializer.dumps(obj) != pickle.dumps(obj, protocol=None)


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


class TestResolveSerializer:
    """Tests for `_resolve_serializer`."""

    def test_serializer_instance_passes_through(self) -> None:
        """Test that a serializer instance is returned unchanged."""
        serializer = JsonSerializer()

        assert _resolve_serializer(serializer) is serializer

    def test_model_becomes_pydantic_serializer(self) -> None:
        """Test that a model type is wrapped in a PydanticSerializer."""
        serializer = _resolve_serializer(_User)

        assert isinstance(serializer, PydanticSerializer)
        assert serializer.loads(b'{"id": 1, "name": "Alice"}').name == "Alice"

    def test_generic_alias_becomes_pydantic_serializer(self) -> None:
        """Test that a parameterized type is wrapped too."""
        serializer = _resolve_serializer(list[_User])

        assert serializer.loads(b'[{"id": 1, "name": "Alice"}]') == [
            _User(id=1, name="Alice")
        ]

    def test_serializer_class_names_the_fix(self) -> None:
        """Test that a serializer class raises and asks for an instance."""
        with pytest.raises(
            TypeError, match=r"pass an instance: JsonSerializer\(\)"
        ):
            _resolve_serializer(JsonSerializer)


class TestInferSerializer:
    """Tests for `_infer_serializer` and its two readers."""

    def test_model_infers_pydantic_serializer(self) -> None:
        """Test that a model type infers a PydanticSerializer."""
        assert isinstance(_infer_serializer(_User), PydanticSerializer)

    @pytest.mark.parametrize("annotation", [bytes, Any, T])
    def test_raw_bytes_annotations_infer_nothing(
        self, annotation: object
    ) -> None:
        """Test that `bytes`, `Any`, and a type variable infer nothing."""
        assert _infer_serializer(annotation) is None

    def test_unsupported_type_infers_nothing(self) -> None:
        """Test that a type Pydantic cannot adapt infers nothing."""

        class Opaque:
            def __init__(self, value: object) -> None:
                self.value = value

        assert _infer_serializer(Opaque) is None

    def test_instance_without_type_parameter_infers_nothing(self) -> None:
        """Test that an unparameterized instance infers nothing."""
        assert _infer_serializer_from_instance(object()) is None

    def test_instance_type_parameter_is_read(self) -> None:
        """Test that `Klass[Model](...)` exposes its parameter."""
        assert isinstance(
            _infer_serializer_from_instance(PydanticSerializer[_User](_User)),
            PydanticSerializer,
        )

    def test_class_without_matching_base_infers_nothing(self) -> None:
        """Test that a subclass of another generic infers nothing."""
        assert (
            _infer_serializer_from_class(list[int], PydanticSerializer) is None
        )

    def test_class_type_parameter_is_read(self) -> None:
        """Test that `class Sub(Klass[Model])` exposes its parameter."""

        class UserSerializer(PydanticSerializer[_User]):
            pass

        assert isinstance(
            _infer_serializer_from_class(UserSerializer, PydanticSerializer),
            PydanticSerializer,
        )
