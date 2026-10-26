"""The error responses of the Litestar integration.

`grelmicro.integrations.litestar` loads this module when an app renders
its errors through grelmicro, and when `error_response` is first read.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Annotated, Any, cast

from litestar.exceptions import HTTPException
from typing_extensions import Doc

from grelmicro.http import ErrorResponses, merge_headers
from grelmicro.http._kinds import BODYLESS_STATUSES, HANDLED
from grelmicro.http._openapi import add_error_schema
from grelmicro.integrations._litestar_openapi import add_openapi_edit
from grelmicro.integrations.litestar import _field_errors

if TYPE_CHECKING:
    from collections.abc import Iterable

    from litestar import Litestar, Request
    from litestar.response import Response

__all__ = ["answer_for", "error_response", "wire_error_responses"]


def wire_error_responses(app: Litestar, errors: ErrorResponses) -> None:
    """Render grelmicro rejections and Litestar's own errors with `errors`.

    `install_error_responses` describes what this registers.
    """

    def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection, taking the occurrence from the request path."""
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
        )

    def http_error(request: Request, exc: Exception) -> Response:
        """Reshape Litestar's own error into the registered format."""
        from litestar.exceptions import (  # noqa: PLC0415
            ValidationException,
        )
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        http_exc = cast("HTTPException", exc)
        headers = http_exc.headers or {}
        if http_exc.status_code in BODYLESS_STATUSES:
            # A `204` or a `304` carries no body by the protocol, whatever
            # format the app answers in.
            return LitestarResponse(
                content=b"",
                status_code=http_exc.status_code,
                headers=headers,
            )
        if isinstance(exc, ValidationException):
            rendered = errors.render_validation(
                _field_errors(exc),
                status=http_exc.status_code,
                detail=http_exc.detail or None,
                instance=request.url.path,
            )
        else:
            rendered = errors.render_status(
                http_exc.status_code,
                detail=http_exc.detail or None,
                instance=request.url.path,
            )
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, headers),
        )

    app.state.grelmicro_error_responses = errors

    if HTTPException not in app.exception_handlers:
        app.exception_handlers[HTTPException] = http_error
        # Only now is what the framework describes no longer what the app
        # answers with. An app that kept its own handler keeps its own
        # schema too, or the two would disagree.
        _document_error_responses(app, errors)

    for klass in HANDLED:
        # A handler passed to `Litestar(exception_handlers=...)` wins, the
        # same way one registered before `install` does on Starlette.
        if klass not in app.exception_handlers:
            app.exception_handlers[klass] = handler


def answer_for(app: Litestar, handled: Iterable[type[Exception]]) -> None:
    """Register a handler for each rejection a component answers itself.

    The format is read per request from whichever `ErrorResponses` the app
    registered, and is RFC 9457 when it registered none. A handler passed
    to `Litestar(exception_handlers=...)` wins, and so does the one
    `install_error_responses` registered, which renders the same way.
    """

    def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection in the format the app answers in."""
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        errors = (
            getattr(request.app.state, "grelmicro_error_responses", None)
            or ErrorResponses()
        )
        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
        )

    for klass in handled:
        if klass not in app.exception_handlers:
            app.exception_handlers[klass] = handler


_LITESTAR_ERROR_MEMBERS = frozenset({"detail", "status_code"})
"""What every schema Litestar generates for its own exception requires.

Matched on the shape rather than on the description text, which is drawn
from a class docstring and is not a contract.
"""


def _document_error_responses(app: Litestar, errors: ErrorResponses) -> None:
    """Republish the schema's error responses in the format now installed.

    Litestar describes every error response with the shape of its own
    `HTTPException`. That is no longer what those operations answer with, so
    a generated client would decode the wrong body.

    Applied to `app.openapi_schema` now, with every edit `install` added
    before it, so the schema says it before the app starts. Applied again
    to each schema Litestar builds anew when a route is registered later,
    before or after the app started.
    """
    add_openapi_edit(
        app,
        functools.partial(_rewrite_error_responses, errors=errors),
        key="error_responses",
    )


def _rewrite_error_responses(
    schema: dict[str, Any], errors: ErrorResponses
) -> None:
    """Point every response Litestar generated at the registered body.

    The component is published through the shared helper, so a model the
    app already declared under that name is not replaced by ours.
    """
    targets = [
        response
        for path_item in schema.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for response in operation.get("responses", {}).values()
        if _is_generated(response)
    ]
    if not targets:
        # Nothing answers with the model, so publishing it would leave the
        # schema carrying a component nothing points at.
        return
    ref = add_error_schema(schema, errors.model)
    if not ref:
        # Both names are taken by the app's own models. Litestar's own
        # entry is a better answer than one pointing at nothing.
        return
    for response in targets:
        response["content"] = {errors.media_type: {"schema": {"$ref": ref}}}


def _is_generated(response: dict[str, Any]) -> bool:
    """Return whether Litestar described this response with its own shape.

    A response the app declared itself, and one with no body at all, has no
    schema requiring Litestar's members and is left alone.
    """
    generated = (
        (response.get("content") or {})
        .get("application/json", {})
        .get("schema", {})
    )
    return set(generated.get("required", ())) >= _LITESTAR_ERROR_MEMBERS


def error_response(
    request: Annotated[
        Request,
        Doc("The request being answered, which knows its app."),
    ],
    *,
    status: Annotated[int, Doc("HTTP status code of the response.")],
    detail: Annotated[
        str | None,
        Doc("Explanation of this occurrence, safe to show a client."),
    ] = None,
    extensions: Annotated[
        dict[str, Any] | None,
        Doc("Extra members to carry, where the format has room for them."),
    ] = None,
) -> Response:
    """Answer from your own exception handler in the app's error format.

    The Litestar counterpart of the Starlette helper. The format is read
    from the app, so a service that registered `ErrorResponses.tmf()`
    answers in TMF from here too.

    ```python
    from grelmicro.integrations.litestar import error_response


    def handle(request: Request, exc: InsufficientFunds) -> Response:
        return error_response(
            request, status=409, detail="Not enough to cover this charge."
        )
    ```
    """
    from litestar.response import (  # noqa: PLC0415
        Response as LitestarResponse,
    )

    errors = (
        getattr(request.app.state, "grelmicro_error_responses", None)
        or ErrorResponses()
    )
    rendered = errors.render_status(
        status,
        detail=detail,
        instance=request.url.path,
        extensions=extensions,
    )
    return LitestarResponse(
        content=rendered.body,
        status_code=rendered.status,
        media_type=rendered.media_type,
        headers=rendered.headers,
    )


error_response.__module__ = "grelmicro.integrations.litestar"
