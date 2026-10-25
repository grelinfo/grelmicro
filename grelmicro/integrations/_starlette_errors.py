"""The error responses of the Starlette integration.

`grelmicro.integrations.starlette` loads this module when an app renders
its errors through grelmicro, and when `error_response` is first read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from grelmicro.http import ErrorResponses, merge_headers
from grelmicro.http._kinds import BODYLESS_STATUSES, HANDLED
from grelmicro.integrations.starlette import HTTP_422_UNPROCESSABLE_CONTENT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["answer_for", "error_response", "wire_error_responses"]


def wire_error_responses(app: Starlette, errors: ErrorResponses) -> None:
    """Render grelmicro rejections and the framework's own errors with `errors`.

    `install_error_responses` describes what this registers.
    """
    # Recorded so `error_response` reads the format the app actually
    # answers in, rather than assuming RFC 9457.
    app.state.grelmicro_error_responses = errors

    async def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection, taking the occurrence from the request path."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
        )

    async def http_error(request: Request, exc: Exception) -> Response:
        """Reshape the framework's own error into the registered format."""
        from starlette.responses import Response  # noqa: PLC0415

        http_exc = cast("HTTPException", exc)
        headers = http_exc.headers or {}
        if http_exc.status_code in BODYLESS_STATUSES:
            # A `204` or a `304` carries no body by the protocol, whatever
            # format the app answers in. Starlette guards this in its own
            # handler and so must this one.
            return Response(status_code=http_exc.status_code, headers=headers)
        detail = http_exc.detail
        rendered = errors.render_status(
            http_exc.status_code,
            detail=detail if isinstance(detail, str) else None,
            extensions=_structured_detail(detail),
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, headers),
        )

    async def validation_error(
        request: Request,
        exc: Exception,
    ) -> Response:
        """Reshape a request that did not match the endpoint's shape."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render_validation(
            _field_errors(exc),
            status=HTTP_422_UNPROCESSABLE_CONTENT,
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    _install_framework_handlers(app, http_error, validation_error)

    for klass in HANDLED:
        # A handler the app registered first wins. Registering before
        # `install` is the natural order (build the app, register handlers,
        # wire grelmicro), and overwriting there would take a handler away
        # from an app that upgrades, without saying so.
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


def answer_for(app: Starlette, handled: Iterable[type[Exception]]) -> None:
    """Register a handler for the rejections a component answers itself.

    A component that carries `handled_exceptions()` names the rejections
    its registration is the opt-in for, so a service that asked for them
    gets the status on the wire rather than a `500`. The format is read
    per request from whichever `ErrorResponses` the app registered, and is
    RFC 9457 when it registered none.

    A handler the app registered first wins, and so does the one
    `install_error_responses` registered, which renders the same way.

    A rejection carrying `headers` puts them on the answer, so what the
    refusal has to tell the client, the quota it just spent above all,
    travels with the status.
    """

    async def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection in the format the app answers in."""
        from starlette.responses import Response  # noqa: PLC0415

        errors = (
            getattr(request.app.state, "grelmicro_error_responses", None)
            or ErrorResponses()
        )
        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
        )

    for klass in handled:
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


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

    Writing a handler of your own is how one error opts out of the shape
    grelmicro installs. This is for the other case: you want your own
    handler and the same shape as everything else.

    ```python
    from grelmicro.integrations.fastapi import error_response


    @app.exception_handler(InsufficientFunds)
    async def handle(request: Request, exc: InsufficientFunds) -> Response:
        return error_response(
            request,
            status=409,
            detail="The account does not hold enough to cover this charge.",
            extensions={"balance": exc.balance},
        )
    ```

    The format is read from the app, so a service that registered
    `ErrorResponses.tmf()` answers in TMF from here too, with no second
    place to keep in step. An app that registered nothing gets RFC 9457.
    """
    from starlette.responses import Response  # noqa: PLC0415

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
    return Response(
        content=rendered.body,
        status_code=rendered.status,
        media_type=rendered.media_type,
        headers=rendered.headers,
    )


def _framework_errors() -> list[tuple[type[Exception], bool]]:
    """Return the framework's own errors, paired with whether they validate.

    Registering these is what makes the whole API answer in one shape. An
    `HTTPException` a handler raises and a request that failed validation
    are errors the client caused, so they belong in the same format as a
    rejection.
    """
    from starlette.exceptions import HTTPException  # noqa: PLC0415

    errors: list[tuple[type[Exception], bool]] = [(HTTPException, False)]
    try:
        from fastapi.exceptions import (  # noqa: PLC0415
            RequestValidationError,
        )
    except ImportError:  # pragma: no cover
        # A plain Starlette app validates nothing, so there is no such
        # error to reshape.
        return errors
    errors.append((RequestValidationError, True))
    return errors


def _is_framework_default(app: Starlette, klass: type[Exception]) -> bool:
    """Return whether the handler for `klass` is the one FastAPI installed.

    FastAPI registers its own handlers in the constructor, so "already
    registered" cannot mean "the app chose this". Only a handler the app
    put there is left alone, which is how it opts one error back out of the
    shared format.

    A plain Starlette app registers nothing, so a missing entry is the same
    answer as a default one.
    """
    registered = app.exception_handlers.get(klass)
    if registered is None:
        return True
    try:
        from fastapi.exception_handlers import (  # noqa: PLC0415
            http_exception_handler,
            request_validation_exception_handler,
        )
    except ImportError:  # pragma: no cover
        return False
    return registered in (
        http_exception_handler,
        request_validation_exception_handler,
    )


def _field_errors(exc: Exception) -> list[dict[str, Any]]:
    """Return the field errors of a validation failure, without the input.

    `loc`, `msg` and `type` tell a client which part of the request to fix.
    `input` only repeats what the client just sent, and `ctx` carries an
    exception object that serializes to nothing useful. Both are dropped.
    """
    raw = getattr(exc, "errors", None)
    entries = raw() if callable(raw) else []
    return [
        {key: value for key, value in entry.items() if key in _FIELD_KEYS}
        for entry in entries
    ]


_FIELD_KEYS = frozenset({"loc", "msg", "type"})
"""Members of a field error that help a client fix its request."""


def _install_framework_handlers(
    app: Starlette,
    http_error: Callable[..., Any],
    validation_error: Callable[..., Any],
) -> None:
    """Reshape the framework's own errors into the registered format.

    A handler the app registered itself is left alone. The one grelmicro
    installs for validation is recorded, so anything that documents the
    schema can tell whether grelmicro is still the one answering.
    """
    for klass, validates in _framework_errors():
        if not _is_framework_default(app, klass):
            continue
        handler = validation_error if validates else http_error
        app.add_exception_handler(klass, handler)
        if validates:
            app.state.grelmicro_validation_handler = handler


def _structured_detail(detail: Any) -> dict[str, Any] | None:  # noqa: ANN401
    """Return the extension members a non-string `detail` becomes.

    FastAPI documents a dict or a list there, and dropping it would lose a
    payload the app meant the client to read.

    A mapping is what an extension member already is, so its entries are
    carried as members in their own right. Anything else goes under
    `errors`, the name RFC 9457 readers expect a list of sub-problems
    under. A name that would displace one of the five standard members is
    dropped by `build`, so merging is safe.
    """
    if detail is None or isinstance(detail, str):
        return None
    if isinstance(detail, dict):
        return dict(detail)
    return {"errors": detail}
