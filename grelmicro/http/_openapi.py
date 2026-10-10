"""Publishing the error body in an OpenAPI schema.

Shared by every integration that generates one, so a schema says the same
thing about the same app whichever framework built it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from grelmicro._paths import path_format
from grelmicro.http._problem import PROBLEM_MEDIA_TYPE, ProblemDetail

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from pydantic import BaseModel

    from grelmicro.http._component import ErrorResponses
    from grelmicro.http._routes import RouteDeclaration


@runtime_checkable
class DescribesItself(Protocol):
    """A component that describes itself in the OpenAPI schema it is handed."""

    def _document_openapi(
        self,
        schema: dict[str, Any],
        *,
        routes: Iterable[RouteDeclaration] = (),
        errors: ErrorResponses | None = None,
    ) -> dict[str, Any]: ...


def describing(components: Iterable[object]) -> list[DescribesItself]:
    """Return the components that describe themselves, in order."""
    return [
        component
        for component in components
        if isinstance(component, DescribesItself)
    ]


def describe_schema(
    schema: dict[str, Any],
    components: Iterable[DescribesItself],
    *,
    routes: list[RouteDeclaration],
    errors: ErrorResponses | None,
) -> None:
    """Hand the schema to each component, which edits it in place."""
    for component in components:
        component._document_openapi(  # noqa: SLF001
            schema, routes=routes, errors=errors
        )


def add_error_schema(schema: dict[str, Any], model: type[BaseModel]) -> str:
    """Publish the problem body component and return the ref that points at it.

    An app may already publish a model of its own under the same name, in
    which case pointing the middleware's responses at it would hand a
    generated client the wrong shape to decode. The component is compared
    before it is reused, and a different one is published beside it under a
    qualified name rather than replacing what the app declared.
    """
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    ours = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name in (model.__name__, f"Grelmicro{model.__name__}"):
        existing = schemas.get(name)
        if existing is None:
            schemas[name] = ours
            return f"#/components/schemas/{name}"
        if _same_model(existing, ours):
            return f"#/components/schemas/{name}"
    # Both names are taken by something else, which takes a deliberate act.
    # Say nothing about the body rather than name the wrong shape.
    return ""


def _same_model(published: dict[str, Any], ours: dict[str, Any]) -> bool:
    """Return whether a published component describes the model we would add.

    Compared by what identifies the model rather than by the whole
    rendering. A framework that already published the very same class
    renders it slightly differently, FastAPI writing an explicit
    `default: None` where `model_json_schema` writes nothing, and treating
    that as a different model publishes the same body twice under two
    names. Which is what happens to anyone following the documented
    `responses={429: {"model": ProblemDetail}}`.
    """
    return (
        all(published.get(key) == ours.get(key) for key in ("title", "type"))
        and all(
            sorted(published.get(key) or ()) == sorted(ours.get(key) or ())
            for key in ("required",)
        )
        and sorted(published.get("properties") or ())
        == sorted(ours.get("properties") or ())
    )


def referenced(node: object) -> set[str]:
    """Return the component names anything under `node` points at."""
    if isinstance(node, dict):
        found: set[str] = set()
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value.rsplit("/", 1)[-1])
            else:
                found |= referenced(value)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= referenced(item)
        return found
    return set()


def error_format(errors: ErrorResponses | None) -> tuple[str, type[BaseModel]]:
    """Return the media type and the model a refusal is answered with.

    The registered `ErrorResponses` decides, and RFC 9457 problem details
    are the answer without one.
    """
    if errors is None:
        return PROBLEM_MEDIA_TYPE, ProblemDetail
    return errors.media_type, errors.model


def declared_operations(
    routes: Iterable[RouteDeclaration],
) -> dict[tuple[str, str | None], RouteDeclaration]:
    """Index each declaration by the path the schema publishes and its method.

    The path drops each parameter's converter, as the schema names it, and
    the method is in lower case, as a path item keys it. A declaration
    answering every method is indexed under `None`.
    """
    found: dict[tuple[str, str | None], RouteDeclaration] = {}
    for declaration in routes:
        path = path_format(declaration.path)
        for method in declaration.methods or (None,):
            key = (path, None if method is None else method.lower())
            found.setdefault(key, declaration)
    return found


def declaration_of(
    declared: dict[tuple[str, str | None], RouteDeclaration],
    path: str,
    method: str,
) -> RouteDeclaration | None:
    """Return the declaration of one operation, or `None` when no route declares it.

    A declaration of this method wins over one answering every method.
    """
    return declared.get((path, method)) or declared.get((path, None))


class Operation(NamedTuple):
    """One operation of a schema, with the path item that holds it."""

    path: str
    path_item: dict[str, Any]
    operation: dict[str, Any]
    method: str


def operations_of(
    schema: dict[str, Any],
    methods: Collection[str] | None = None,
) -> list[Operation]:
    """Return each operation of these methods, with its path item, path and method.

    Only `paths`: a webhook is a request the app sends, and no header a
    middleware reads reaches it. The method is in lower case. `None` takes
    every method.
    """
    return [
        Operation(path, path_item, operation, method.lower())
        for path, path_item in (schema.get("paths") or {}).items()
        for method, operation in path_item.items()
        if isinstance(operation, dict)
        and (methods is None or method.lower() in methods)
    ]


def add_parameter(
    operation: dict[str, Any],
    path_item: dict[str, Any],
    parameter: dict[str, Any],
) -> None:
    """Add the header parameter unless the operation already declares it.

    OpenAPI keys a parameter by name and location, and forbids the same
    pair twice, so a declaration already present at either level wins.
    """
    name = parameter["name"].lower()
    declared = [
        *operation.get("parameters", ()),
        *path_item.get("parameters", ()),
    ]
    if any(
        existing.get("in") == "header"
        and str(existing.get("name", "")).lower() == name
        for existing in declared
    ):
        return
    # A copy per operation, so post-processing one never edits the rest.
    operation.setdefault("parameters", []).append(dict(parameter))


def mark_required(operation: dict[str, Any], name: str, note: str) -> None:
    """Mark a header the operation already declares as required.

    The description gains `note`, for what `required` alone cannot say.
    """
    lowered = name.lower()
    for parameter in operation.get("parameters", ()):
        if (
            parameter.get("in") == "header"
            and str(parameter.get("name", "")).lower() == lowered
        ):
            parameter["required"] = True
            description = str(parameter.get("description", ""))
            if note not in description:
                parameter["description"] = f"{description} {note}".strip()


def merge_response(
    operation: dict[str, Any],
    status: str,
    description: str,
    ref: str,
    media_type: str,
) -> None:
    """Describe a status the middleware returns, keeping what is there.

    A response the operation already declares, such as the `422` a
    framework generates for request validation, keeps its schema and gains
    this description and the error media type beside it. A second call
    adds nothing.
    """
    responses = operation.setdefault("responses", {})
    existing = responses.get(status)
    if existing is None:
        responses[status] = {"description": description}
        existing = responses[status]
    else:
        current = existing.get("description", "")
        if description not in current:
            existing["description"] = f"{current}\n\n{description}".strip()
    if ref:
        existing.setdefault("content", {}).setdefault(
            media_type, {"schema": {"$ref": ref}}
        )
