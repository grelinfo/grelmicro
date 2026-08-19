"""Publishing the error body in an OpenAPI schema.

Shared by every integration that generates one, so a schema says the same
thing about the same app whichever framework built it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel


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
