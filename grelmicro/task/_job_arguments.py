"""How a job's arguments travel: as JSON, rebuilt from the body's type hints."""

from __future__ import annotations

import inspect
import json
from annotationlib import ForwardRef
from typing import TYPE_CHECKING, Any, Unpack, get_args, get_origin

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic.errors import PydanticSchemaGenerationError
from pydantic_core import PydanticSerializationError

if TYPE_CHECKING:
    from collections.abc import Callable


def resolve_annotation(
    annotation: object, function: Callable[..., Any]
) -> object:
    """Resolve an annotation written as a string or a forward reference.

    Raises:
        NameError: If it names something the runtime cannot resolve.
    """
    if isinstance(annotation, str):
        annotation = ForwardRef(annotation, owner=function)
    if isinstance(annotation, ForwardRef):
        return annotation.evaluate(owner=function)
    return annotation


class JobArguments:
    """Turns a call into a JSON payload, and a payload back into a call.

    Each parameter after `progress` has a type adapter built from its type
    hint. A hint naming something not defined yet gets its adapter at first
    use. A parameter with no type hint, or one still only a type checker can
    resolve by then, travels as plain JSON.
    """

    __slots__ = ("_adapters", "_function", "_name", "_parameters", "_signature")

    def __init__(
        self,
        name: str,
        function: Callable[..., Any],
        signature: inspect.Signature,
    ) -> None:
        """Build the adapters of `signature`, whose first parameter is `progress`.

        Raises:
            TypeError: If a parameter's type resolves and has no JSON form.
        """
        self._name = name
        self._function = function
        self._signature = signature
        parameters = list(signature.parameters.values())[1:]
        self._parameters = {
            parameter.name: parameter for parameter in parameters
        }
        self._adapters = {
            parameter.name: adapter
            for parameter in parameters
            if (adapter := _adapter(name, parameter, function)) is not None
        }

    def encode(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        """Return the call as a JSON payload.

        Raises:
            TypeError: If the body does not take these arguments, a value
                does not match its type hint or has no JSON form, or a
                parameter's type has no JSON form.
        """
        try:
            bound = self._signature.bind(None, *args, **kwargs)
        except TypeError as exc:
            msg = f"Job {self._name!r} cannot take these arguments: {exc}"
            raise TypeError(msg) from None
        payload = {
            name: self._encode_one(name, value)
            for name, value in bound.arguments.items()
            if name in self._parameters
        }
        return json.dumps(payload)

    def decode(self, payload: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Return the call a payload holds, after `progress`.

        Raises:
            json.JSONDecodeError: If the payload is not JSON.
            pydantic.ValidationError: If a stored value no longer matches its
                type hint.
            TypeError: If a parameter's type has no JSON form.
        """
        stored = json.loads(payload)
        arguments: dict[str, Any] = {
            next(iter(self._signature.parameters)): None
        }
        for parameter in self._signature.parameters.values():
            if parameter.name not in stored:
                continue
            value = self._adapter_of(parameter.name).validate_python(
                stored[parameter.name]
            )
            arguments[parameter.name] = value
        bound = inspect.BoundArguments(self._signature, arguments)
        return bound.args[1:], bound.kwargs

    def _adapter_of(self, name: str) -> TypeAdapter[Any]:
        """Return the adapter of one parameter, building it at first use.

        Raises:
            TypeError: If its type has no JSON form.
        """
        adapter = self._adapters.get(name)
        if adapter is None:
            parameter = self._parameters[name]
            adapter = _adapter(
                self._name, parameter, self._function
            ) or _build_adapter(self._name, parameter, JsonValue, {})
            assert adapter is not None  # noqa: S101
            self._adapters[name] = adapter
        return adapter

    def _encode_one(self, name: str, value: object) -> object:
        """Return one argument as JSON data.

        Raises:
            TypeError: If it does not match its type hint, has no JSON form,
                or would not read back as the same value.
        """
        adapter = self._adapter_of(name)
        try:
            validated = adapter.validate_python(value, strict=True)
            dumped = adapter.dump_python(validated, mode="json")
            json.dumps(dumped, allow_nan=False)
            if adapter.validate_python(dumped) != validated:
                raise ValueError  # noqa: TRY301
        except (
            ValidationError,
            PydanticSerializationError,
            TypeError,
            ValueError,
        ):
            msg = (
                f"Job {self._name!r} takes JSON arguments, and {name!r} got "
                f"a {type(value).__name__} that does not fit. Pass something "
                "the body can load it from, such as an id"
            )
            raise TypeError(msg) from None
        return dumped


def _adapter(
    job_name: str, parameter: inspect.Parameter, function: Callable[..., Any]
) -> TypeAdapter[Any] | None:
    """Return the adapter of one parameter.

    `None` while its type hint names something not defined yet.

    Raises:
        TypeError: If its type has no JSON form.
    """
    try:
        annotation: Any = resolve_annotation(parameter.annotation, function)
    except NameError:
        return None
    if annotation is inspect.Parameter.empty:
        annotation = JsonValue
    names = getattr(inspect.unwrap(function), "__globals__", {})
    return _build_adapter(job_name, parameter, annotation, names)


def _unpacked(annotation: object) -> object | None:
    """Return the type inside `Unpack[...]` or `*tuple[...]`, else `None`."""
    if get_origin(annotation) is Unpack:
        return get_args(annotation)[0]
    if getattr(annotation, "__unpacked__", False):
        return tuple.__class_getitem__(get_args(annotation))
    return None


def _build_adapter(
    job_name: str,
    parameter: inspect.Parameter,
    annotation: Any,  # noqa: ANN401
    names: dict[str, Any],
) -> TypeAdapter[Any] | None:
    """Return the adapter of `annotation`, resolving nested names from `names`.

    `None` while a nested name is not defined yet.

    Raises:
        TypeError: If the type has no JSON form.
    """
    unpacked = _unpacked(annotation)
    if unpacked is not None:
        annotation = unpacked
    elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
        annotation = tuple[annotation, ...]
    elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
        annotation = dict[str, annotation]
    try:
        adapter = TypeAdapter(annotation)
        if not adapter.pydantic_complete:
            adapter.rebuild(_types_namespace=names, raise_errors=False)
    except PydanticSchemaGenerationError:
        msg = (
            f"Job {job_name!r} takes JSON arguments, and {parameter.name!r} has "
            f"the type {parameter.annotation!r}, which has no JSON form. "
            "Pass something the body can load it from, such as an id"
        )
        raise TypeError(msg) from None
    return adapter if adapter.pydantic_complete else None
